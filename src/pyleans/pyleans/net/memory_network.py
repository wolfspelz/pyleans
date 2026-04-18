"""InMemoryNetwork — test-only INetwork adapter simulating TCP in-process.

Satisfies the testing half of ``adr-network-port-for-testability``. One
``InMemoryNetwork`` instance per test; servers register under ``(host, port)``;
``open_connection`` wires a pair of in-memory streams to the server's handler
without ever touching the OS network stack.

The simulator reproduces every asyncio-networking behavior pyleans' runtime
depends on: ordered per-connection delivery, real ``drain()`` backpressure
over a bounded write buffer, ``asyncio.Server``-parity lifecycle, and
standard stdlib exception types on failure. Failure-injection hooks let
tests deterministically reproduce peer reset / peer close / refused
connection scenarios that are flaky against real sockets.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import ssl as ssl_module
from asyncio import StreamReader, StreamReaderProtocol, StreamWriter
from typing import TYPE_CHECKING, cast

from pyleans.net.network import (
    ClientConnectedCallback,
    INetwork,
    NetworkServer,
    SocketInfo,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

_DEFAULT_HIGH_WATER = 64 * 1024
_DEFAULT_LOW_WATER = 16 * 1024
_READER_LIMIT = 2**20
_VIRTUAL_PORT_START = 40000


class _MemorySocketInfo:
    """SocketInfo stub reporting the server's virtual ``(host, port)``."""

    def __init__(self, address: tuple[str, int]) -> None:
        self._address = address

    def getsockname(self) -> tuple[str, int]:
        return self._address


class _MemoryStreamReader(StreamReader):
    """StreamReader that notifies its peer transport on consumption.

    Pure ``StreamReader`` subclass — clients use it as an ordinary reader.
    Intercepts ``read``/``readexactly``/``readline`` so the paired
    ``_MemoryTransport`` can update its outbound-buffer accounting and
    release drain waiters.
    """

    def __init__(
        self,
        *,
        limit: int = _READER_LIMIT,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(limit=limit, loop=loop)
        self._peer_transport: _MemoryTransport | None = None

    def _attach_peer_transport(self, transport: _MemoryTransport) -> None:
        self._peer_transport = transport

    def _notify_peer(self, length: int) -> None:
        if length and self._peer_transport is not None:
            self._peer_transport.notify_consumed(length)

    async def read(self, n: int = -1) -> bytes:
        data = await super().read(n)
        self._notify_peer(len(data))
        return data

    async def readexactly(self, n: int) -> bytes:
        data = await super().readexactly(n)
        self._notify_peer(len(data))
        return data

    async def readline(self) -> bytes:
        data = await super().readline()
        self._notify_peer(len(data))
        return data


# pylint: disable-next=too-many-public-methods
class _MemoryTransport(asyncio.Transport):  # implements full Transport contract
    """Virtual transport backing :class:`MemoryStreamWriter`.

    ``write(data)`` forwards bytes to the peer's :class:`_MemoryStreamReader`.
    Manages its own drain waiter so backpressure above ``high_water`` holds
    the writer until the peer consumes down past ``low_water``.
    """

    def __init__(
        self,
        peer_reader: _MemoryStreamReader,
        *,
        sockname: tuple[str, int],
        peername: tuple[str, int],
        high_water: int = _DEFAULT_HIGH_WATER,
        low_water: int = _DEFAULT_LOW_WATER,
    ) -> None:
        if low_water >= high_water:
            raise ValueError("low_water must be below high_water")
        super().__init__(extra={"peername": peername, "sockname": sockname})
        self._peer_reader = peer_reader
        self._high_water = high_water
        self._low_water = low_water
        self._buffered = 0
        self._closing = False
        self._reset_pending = False
        self._protocol: StreamReaderProtocol | None = None
        self._drain_waiter: asyncio.Future[None] | None = None

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        if not isinstance(protocol, StreamReaderProtocol):
            raise TypeError("protocol must be a StreamReaderProtocol")
        self._protocol = protocol

    def get_protocol(self) -> asyncio.BaseProtocol:
        if self._protocol is None:
            raise RuntimeError("protocol not set")
        return self._protocol

    @property
    def reset_pending(self) -> bool:
        return self._reset_pending

    @property
    def buffered(self) -> int:
        return self._buffered

    @property
    def is_over_high_water(self) -> bool:
        return self._buffered >= self._high_water

    async def await_drain(self) -> None:
        """Block until the peer has consumed past the low-water mark."""
        if self._reset_pending:
            raise ConnectionResetError("Connection reset by peer")
        if not self.is_over_high_water:
            return
        loop = asyncio.get_running_loop()
        self._drain_waiter = loop.create_future()
        try:
            await self._drain_waiter
        finally:
            self._drain_waiter = None

    def trigger_reset(self) -> None:
        """Arm the reset flag and surface it to any in-flight drain waiter."""
        self._reset_pending = True
        if self._drain_waiter is not None and not self._drain_waiter.done():
            self._drain_waiter.set_exception(ConnectionResetError("Connection reset by peer"))

    def feed_peer_eof(self) -> None:
        """Signal EOF to the peer reader (simulates this side closing its write end)."""
        self._peer_reader.feed_eof()

    def write(self, data: bytes | bytearray | memoryview) -> None:
        if self._reset_pending:
            raise ConnectionResetError("Connection reset by peer")
        if self._closing:
            raise ConnectionResetError("Connection is closed")
        payload = bytes(data)
        if not payload:
            return
        self._buffered += len(payload)
        self._peer_reader.feed_data(payload)

    def writelines(self, list_of_data: Iterable[bytes | bytearray | memoryview]) -> None:
        for chunk in list_of_data:
            self.write(chunk)

    def notify_consumed(self, n: int) -> None:
        """Callback from the peer reader after it consumed ``n`` bytes."""
        self._buffered = max(0, self._buffered - n)
        if (
            self._buffered <= self._low_water
            and self._drain_waiter is not None
            and not self._drain_waiter.done()
        ):
            self._drain_waiter.set_result(None)

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._peer_reader.feed_eof()
        if self._protocol is not None:
            loop = asyncio.get_event_loop()
            loop.call_soon(self._protocol.connection_lost, None)

    def abort(self) -> None:
        self.close()

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        if self._closing:
            return
        self._peer_reader.feed_eof()

    def pause_reading(self) -> None:
        # The simulator feeds data directly into the peer reader — no read
        # side to pause. Implemented as a no-op for Transport contract parity.
        return

    def resume_reading(self) -> None:
        return

    def is_reading(self) -> bool:
        return not self._closing

    def get_write_buffer_size(self) -> int:
        return self._buffered

    def get_write_buffer_limits(self) -> tuple[int, int]:
        return self._low_water, self._high_water

    def set_write_buffer_limits(self, high: int | None = None, low: int | None = None) -> None:
        if high is None:
            high = self._high_water
        if low is None:
            low = high // 4
        if low >= high:
            raise ValueError("low must be less than high")
        self._high_water = high
        self._low_water = low


class MemoryStreamWriter(StreamWriter):
    """StreamWriter over :class:`_MemoryTransport` — same API plus test hooks.

    Behaves as an ordinary ``asyncio.StreamWriter`` for production code;
    tests can additionally call :meth:`simulate_reset` and
    :meth:`simulate_peer_close` to drive failure paths deterministically.
    """

    @property
    def _mem_transport(self) -> _MemoryTransport:
        return cast(_MemoryTransport, self.transport)

    def simulate_reset(self) -> None:
        """Arm ``ConnectionResetError`` for the next ``write``/``drain``."""
        self._mem_transport.trigger_reset()

    def simulate_peer_close(self) -> None:
        """Signal EOF to the peer reader — next ``readexactly`` raises IncompleteReadError."""
        self._mem_transport.feed_peer_eof()

    def write(self, data: bytes | bytearray | memoryview) -> None:
        if self._mem_transport.reset_pending:
            raise ConnectionResetError("Connection reset by peer")
        super().write(data)

    async def drain(self) -> None:
        await self._mem_transport.await_drain()


def _make_pair(
    *,
    limit: int,
    local: tuple[str, int],
    remote: tuple[str, int],
    high_water: int,
    low_water: int,
    loop: asyncio.AbstractEventLoop,
) -> tuple[_MemoryStreamReader, MemoryStreamWriter, _MemoryTransport]:
    """Create a (reader, writer, transport) triple for one direction."""
    peer_reader = _MemoryStreamReader(limit=limit, loop=loop)
    transport = _MemoryTransport(
        peer_reader,
        sockname=local,
        peername=remote,
        high_water=high_water,
        low_water=low_water,
    )
    protocol = StreamReaderProtocol(peer_reader, loop=loop)
    transport.set_protocol(protocol)
    writer = MemoryStreamWriter(transport, protocol, peer_reader, loop)
    return peer_reader, writer, transport


class InMemoryServer(NetworkServer):
    """Simulated ``asyncio.Server`` — accepts connections until ``close()``."""

    def __init__(
        self,
        address: tuple[str, int],
        handler: ClientConnectedCallback,
        owner: InMemoryNetwork,
    ) -> None:
        self._address = address
        self._handler = handler
        self._owner = owner
        self._closed = False
        self._handler_tasks: set[asyncio.Task[None]] = set()

    @property
    def address(self) -> tuple[str, int]:
        return self._address

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner._unregister(self._address)

    async def wait_closed(self) -> None:
        if self._handler_tasks:
            await asyncio.gather(*self._handler_tasks, return_exceptions=True)

    @property
    def sockets(self) -> list[SocketInfo]:
        return [_MemorySocketInfo(self._address)]

    def handle_incoming(self, reader: StreamReader, writer: MemoryStreamWriter) -> None:
        """Dispatch an incoming in-memory connection to the handler."""
        if self._closed:
            writer.close()
            return
        task: asyncio.Task[None] = asyncio.ensure_future(self._handler(reader, writer))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)


class InMemoryNetwork(INetwork):
    """In-process network simulator. One instance per test.

    Servers register themselves under ``(host, port)``. ``open_connection``
    looks up the server and wires an in-memory stream pair to the server's
    handler. No OS sockets are created.
    """

    def __init__(
        self,
        *,
        high_water: int = _DEFAULT_HIGH_WATER,
        low_water: int = _DEFAULT_LOW_WATER,
        reader_limit: int = _READER_LIMIT,
    ) -> None:
        if low_water >= high_water:
            raise ValueError("low_water must be below high_water")
        self._servers: dict[tuple[str, int], InMemoryServer] = {}
        self._next_virtual_port = _VIRTUAL_PORT_START
        self._high_water = high_water
        self._low_water = low_water
        self._reader_limit = reader_limit

    async def start_server(
        self,
        client_connected_cb: ClientConnectedCallback,
        host: str,
        port: int,
        *,
        ssl: ssl_module.SSLContext | None = None,
    ) -> NetworkServer:
        if ssl is not None:
            logger.debug("InMemoryNetwork: ssl parameter is ignored (TLS not simulated)")
        if port == 0:
            port = self._allocate_virtual_port()
        key = (host, port)
        if key in self._servers and not self._servers[key].is_closed():
            raise OSError(errno.EADDRINUSE, f"address in use: {host}:{port}")
        server = InMemoryServer(key, client_connected_cb, owner=self)
        self._servers[key] = server
        logger.debug("InMemoryNetwork: registered server at %s:%s", host, port)
        return server

    async def open_connection(
        self,
        host: str,
        port: int,
        *,
        ssl: ssl_module.SSLContext | None = None,
    ) -> tuple[StreamReader, StreamWriter]:
        if ssl is not None:
            logger.debug("InMemoryNetwork: ssl parameter is ignored (TLS not simulated)")
        server = self._servers.get((host, port))
        if server is None or server.is_closed():
            raise ConnectionRefusedError(f"in-memory: no server at {host}:{port}")

        loop = asyncio.get_running_loop()
        client_sockname = ("127.0.0.1", self._allocate_virtual_port())
        server_sockname = (host, port)

        # Direction 1: client writes → server reads
        server_reader, client_writer, client_transport = _make_pair(
            limit=self._reader_limit,
            local=client_sockname,
            remote=server_sockname,
            high_water=self._high_water,
            low_water=self._low_water,
            loop=loop,
        )

        # Direction 2: server writes → client reads
        client_reader, server_writer, server_transport = _make_pair(
            limit=self._reader_limit,
            local=server_sockname,
            remote=client_sockname,
            high_water=self._high_water,
            low_water=self._low_water,
            loop=loop,
        )

        # Wire each reader back to its peer's transport so reads release drain waiters.
        server_reader._attach_peer_transport(client_transport)
        client_reader._attach_peer_transport(server_transport)

        server.handle_incoming(server_reader, server_writer)
        return client_reader, client_writer

    def _allocate_virtual_port(self) -> int:
        port = self._next_virtual_port
        self._next_virtual_port += 1
        return port

    def _unregister(self, address: tuple[str, int]) -> None:
        self._servers.pop(address, None)

    def active_addresses(self) -> list[tuple[str, int]]:
        """Return addresses of currently-bound simulated servers (for debugging)."""
        return [addr for addr, srv in self._servers.items() if not srv.is_closed()]


__all__ = [
    "InMemoryNetwork",
    "InMemoryServer",
    "MemoryStreamWriter",
]
