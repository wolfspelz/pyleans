"""Network port — pluggable TCP-stream factory.

Every component in pyleans that opens a socket does so through the ``INetwork``
port. Production uses :class:`AsyncioNetwork`, a thin pass-through to
``asyncio.start_server`` / ``asyncio.open_connection``. Tests use
``InMemoryNetwork`` (see task-01-16) so no OS-level ports are bound.

See ``docs/adr/adr-network-port-for-testability.md``.
"""

from __future__ import annotations

import ssl as ssl_module
from abc import ABC, abstractmethod
from asyncio import StreamReader, StreamWriter
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

ClientConnectedCallback = Callable[[StreamReader, StreamWriter], Awaitable[None]]


@runtime_checkable
class SocketInfo(Protocol):
    """Structural protocol for socket-like objects returned by ``NetworkServer.sockets``.

    Duck-compatible with the ``socket.socket`` objects returned by
    ``asyncio.Server.sockets`` — consumers only need ``getsockname()``. The
    return type is kept broad so both real sockets (IPv4 or IPv6) and the
    in-memory simulator satisfy it.
    """

    def getsockname(self) -> Any: ...


class NetworkServer(ABC):
    """Minimal subset of ``asyncio.Server`` every adapter implements.

    Covers exactly what pyleans needs: graceful close, wait-for-close, and
    port discovery via ``.sockets[0].getsockname()``. Extended only when a
    concrete consumer requires more.
    """

    @abstractmethod
    def close(self) -> None:
        """Stop accepting new connections.

        Does not cancel in-flight handler tasks — matches ``asyncio.Server``
        semantics so shutdown-race bugs are not masked.
        """

    @abstractmethod
    async def wait_closed(self) -> None:
        """Wait for in-flight connection handlers to finish."""

    @property
    @abstractmethod
    def sockets(self) -> list[SocketInfo]:
        """Sockets (or sockets-equivalents) the server listens on."""


class INetwork(ABC):
    """Pluggable TCP-stream factory.

    Production uses :class:`AsyncioNetwork`; tests use ``InMemoryNetwork``.
    Consumers accept an optional ``network: INetwork | None`` parameter and
    default to ``AsyncioNetwork()`` if none is passed.
    """

    @abstractmethod
    async def start_server(
        self,
        client_connected_cb: ClientConnectedCallback,
        host: str,
        port: int,
        *,
        ssl: ssl_module.SSLContext | None = None,
    ) -> NetworkServer:
        """Start a server bound to ``(host, port)``.

        ``port=0`` requests an ephemeral (or virtual, for the simulator)
        port; read ``server.sockets[0].getsockname()[1]`` to discover it.
        """

    @abstractmethod
    async def open_connection(
        self,
        host: str,
        port: int,
        *,
        ssl: ssl_module.SSLContext | None = None,
    ) -> tuple[StreamReader, StreamWriter]:
        """Open a client connection to ``(host, port)``.

        Raises :class:`ConnectionRefusedError` when no server is reachable.
        """
