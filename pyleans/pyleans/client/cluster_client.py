"""ClusterClient — lightweight client for calling grains on a remote silo."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from pyleans.errors import TransportError
from pyleans.gateway.protocol import (
    MessageType,
    decode_frame,
    encode_frame,
    read_frame,
)
from pyleans.identity import GrainId

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


class ClusterClient:
    """Connects to a silo gateway and provides grain references.

    Usage::

        client = ClusterClient(gateways=["localhost:30000"])
        await client.connect()
        counter = client.get_grain(CounterGrain, "my-counter")
        value = await counter.get_value()
        await client.close()
    """

    def __init__(
        self,
        gateways: list[str],
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if not gateways:
            raise ValueError("At least one gateway address is required")
        self._gateways = gateways
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._correlation_counter = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._read_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        """Whether the client has an active connection."""
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Connect to the first reachable gateway."""
        last_error: Exception | None = None
        for addr in self._gateways:
            host, port = _parse_address(addr)
            try:
                self._reader, self._writer = await asyncio.open_connection(host, port)
                self._read_task = asyncio.create_task(self._read_loop())
                logger.info("Connected to gateway %s:%s", host, port)
                return
            except OSError as e:
                last_error = e
                logger.debug("Failed to connect to %s:%s: %s", host, port, e)
        raise ConnectionError(f"Could not connect to any gateway: {self._gateways}") from last_error

    async def close(self) -> None:
        """Disconnect from the gateway."""
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None

        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None

        # Fail any pending requests
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Client closed"))
        self._pending.clear()

    def get_grain(self, grain_class: type, key: str) -> RemoteGrainRef:
        """Get a reference to a grain on the remote silo.

        The grain is not activated until the first method call.
        """
        grain_type: str = grain_class._grain_type  # type: ignore[attr-defined]
        grain_id = GrainId(grain_type=grain_type, key=key)
        return RemoteGrainRef(grain_id=grain_id, client=self)

    async def invoke(
        self,
        grain_id: GrainId,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        """Send a grain call to the silo and return the result."""
        if not self.connected:
            raise ConnectionError("Not connected to any gateway")

        self._correlation_counter += 1
        correlation_id = self._correlation_counter

        payload = {
            "grain_type": grain_id.grain_type,
            "key": grain_id.key,
            "method": method,
            "args": args,
            "kwargs": kwargs,
        }
        frame = encode_frame(MessageType.REQUEST, correlation_id, payload)

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[correlation_id] = future

        assert self._writer is not None
        self._writer.write(frame)
        await self._writer.drain()

        try:
            response = await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            self._pending.pop(correlation_id, None)
            raise TimeoutError(
                f"Request {correlation_id} timed out after {self._timeout}s"
            ) from None

        if not response.get("ok"):
            raise TransportError(response.get("error", "Unknown error"))

        return response.get("result")

    async def _read_loop(self) -> None:
        """Read response frames from the gateway and complete pending futures."""
        assert self._reader is not None
        try:
            while True:
                frame_data = await read_frame(self._reader)
                _msg_type, correlation_id, payload = decode_frame(frame_data)
                future = self._pending.pop(correlation_id, None)
                if future is not None and not future.done():
                    future.set_result(payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except asyncio.CancelledError:
            return
        except TransportError as e:
            logger.warning("Protocol error in read loop: %s", e)
        finally:
            # Fail all remaining pending requests
            err = ConnectionError("Connection lost")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(err)
            self._pending.clear()


class RemoteGrainRef:
    """A proxy for a grain that sends calls over the gateway protocol."""

    def __init__(self, grain_id: GrainId, client: ClusterClient) -> None:
        self._grain_id = grain_id
        self._client = client

    @property
    def identity(self) -> GrainId:
        """The grain identity this reference points to."""
        return self._grain_id

    def __getattr__(self, name: str) -> Any:
        """Return an async callable that dispatches to the remote silo."""
        if name.startswith("_"):
            raise AttributeError(name)

        async def _proxy_call(*args: Any, **kwargs: Any) -> Any:
            return await self._client.invoke(self._grain_id, name, list(args), kwargs)

        return _proxy_call

    def __repr__(self) -> str:
        return f"RemoteGrainRef({self._grain_id})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RemoteGrainRef):
            return NotImplemented
        return self._grain_id == other._grain_id

    def __hash__(self) -> int:
        return hash(self._grain_id)


def _parse_address(addr: str) -> tuple[str, int]:
    """Parse 'host:port' into (host, port)."""
    parts = addr.rsplit(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid gateway address (expected host:port): {addr!r}")
    host = parts[0]
    try:
        port = int(parts[1])
    except ValueError as e:
        raise ValueError(f"Invalid port in gateway address: {addr!r}") from e
    return host, port
