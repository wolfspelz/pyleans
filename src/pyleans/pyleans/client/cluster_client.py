"""ClusterClient — lightweight client for calling grains on a remote silo."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pyleans.errors import TransportError
from pyleans.gateway.protocol import (
    MessageType,
    decode_frame,
    encode_frame,
    read_frame,
)
from pyleans.grain import get_grain_type_name
from pyleans.identity import GrainId
from pyleans.net import AsyncioNetwork, INetwork

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RECONNECT_ATTEMPTS = 2

GatewayRefresher = Callable[[], Awaitable[list[str]]]
"""Async callable returning a fresh list of gateway addresses.

When the client holds one, it consults the refresher whenever the current
connection cannot be used — either at :meth:`connect` time if every address
in the current list fails, or in :meth:`invoke` when a call raises
:class:`ConnectionError` mid-flight. This is the knob the counter-client CLI
uses to stay on one cluster across silo deaths: the refresher re-reads the
membership table so a stale gateway is replaced by any other active silo.
"""


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
        *,
        network: INetwork | None = None,
        gateway_refresher: GatewayRefresher | None = None,
        max_reconnect_attempts: int = _DEFAULT_MAX_RECONNECT_ATTEMPTS,
    ) -> None:
        if not gateways and gateway_refresher is None:
            raise ValueError("At least one gateway address or a gateway_refresher is required")
        if max_reconnect_attempts < 0:
            raise ValueError(
                f"max_reconnect_attempts must be >= 0, got {max_reconnect_attempts!r}",
            )
        self._gateways = list(gateways)
        self._timeout = timeout
        self._network = network or AsyncioNetwork()
        self._gateway_refresher = gateway_refresher
        self._max_reconnect_attempts = max_reconnect_attempts
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected_gateway: str | None = None
        self._correlation_counter = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._read_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        """Whether the client has an active connection."""
        return self._writer is not None and not self._writer.is_closing()

    @property
    def connected_gateway(self) -> str | None:
        """Address ``host:port`` of the gateway currently connected to, or ``None``."""
        return self._connected_gateway

    async def connect(self) -> None:
        """Connect to the first reachable gateway.

        If a :class:`GatewayRefresher` is installed, the current gateway list
        is replaced with a fresh one before dialling, and a second refresh is
        attempted if every address in the initial list fails. That lets a
        membership-driven CLI recover from silos that have died or been
        replaced since the last invocation.
        """
        await self._refresh_gateways_if_available()
        last_error = await self._attempt_connect()
        if last_error is None:
            return
        # Every address in the current list failed. If we have a refresher,
        # pull a fresh list and try once more — it may contain silos that came
        # online after the first read, or drop ones that have since died.
        if self._gateway_refresher is not None:
            logger.info(
                "All gateways in %s failed; refreshing and retrying",
                self._gateways,
            )
            await self._refresh_gateways_if_available(force=True)
            last_error = await self._attempt_connect()
            if last_error is None:
                return
        raise ConnectionError(
            f"Could not connect to any gateway: {self._gateways}",
        ) from last_error

    async def _attempt_connect(self) -> Exception | None:
        """Try each gateway in ``self._gateways`` and return the last error, or ``None``."""
        last_error: Exception | None = None
        for addr in self._gateways:
            host, port = _parse_address(addr)
            try:
                self._reader, self._writer = await self._network.open_connection(host, port)
                self._read_task = asyncio.create_task(self._read_loop())
                self._connected_gateway = f"{host}:{port}"
                logger.info("Connected to gateway %s", self._connected_gateway)
                return None
            except OSError as e:
                last_error = e
                logger.debug("Failed to connect to %s:%s: %s", host, port, e)
        return last_error or ConnectionError("no gateways configured")

    async def _refresh_gateways_if_available(self, *, force: bool = False) -> None:
        """Replace ``self._gateways`` with a fresh list from the refresher, if any.

        Called before the initial connect attempt so a stale constructor
        argument doesn't delay discovery of the real cluster state, and again
        after a full round of failures. ``force`` is reserved for the second
        path and is currently identical to the first — kept for readability.
        """
        del force
        if self._gateway_refresher is None:
            return
        try:
            fresh = await self._gateway_refresher()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Gateway refresher raised: %r; keeping existing list", exc)
            return
        if fresh:
            self._gateways = fresh

    async def close(self) -> None:
        """Disconnect from the gateway."""
        gateway_at_close = self._connected_gateway
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

        if gateway_at_close is not None:
            logger.info("Disconnected from gateway %s", gateway_at_close)
        self._connected_gateway = None

    def get_grain(self, grain_class: type, key: str) -> RemoteGrainRef:
        """Get a reference to a grain on the remote silo.

        The grain is not activated until the first method call.
        """
        grain_type = get_grain_type_name(grain_class)
        grain_id = GrainId(grain_type=grain_type, key=key)
        return RemoteGrainRef(grain_id=grain_id, client=self)

    async def invoke(
        self,
        grain_id: GrainId,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        """Send a grain call to the silo and return the result.

        If a :class:`GatewayRefresher` is installed and the request fails with
        a :class:`ConnectionError` (e.g. the gateway died mid-call), the
        client closes the broken connection, asks the refresher for a new
        gateway list, reconnects, and retries — up to
        ``max_reconnect_attempts`` times. Without a refresher the
        ``ConnectionError`` propagates unchanged, preserving the Phase 1
        single-gateway contract.
        """
        attempts_left = 1 + self._max_reconnect_attempts
        last_error: Exception | None = None
        while attempts_left > 0:
            try:
                return await self._invoke_once(grain_id, method, args, kwargs)
            except ConnectionError as exc:
                last_error = exc
                attempts_left -= 1
                if attempts_left <= 0 or self._gateway_refresher is None:
                    raise
                logger.info(
                    "Gateway connection lost during invoke (%s); reconnecting to another silo",
                    exc,
                )
                await self._reconnect_to_fresh_gateway()
        # Unreachable — attempts_left<=0 re-raises above.
        raise last_error or ConnectionError("invoke exhausted retry budget")

    async def _invoke_once(
        self,
        grain_id: GrainId,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        """Single attempt of an invoke — no reconnect, no retry."""
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

        logger.debug(
            "Request %d: %s.%s on %s", correlation_id, grain_id.grain_type, method, grain_id.key
        )
        assert self._writer is not None
        try:
            self._writer.write(frame)
            await self._writer.drain()
        except (ConnectionError, OSError) as exc:
            self._pending.pop(correlation_id, None)
            raise ConnectionError(f"Write to gateway failed: {exc}") from exc

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

    async def _reconnect_to_fresh_gateway(self) -> None:
        """Close the broken connection and reconnect via the gateway refresher."""
        await self.close()
        await self.connect()

    async def _read_loop(self) -> None:
        """Read response frames from the gateway and complete pending futures."""
        assert self._reader is not None
        cancelled = False
        try:
            while True:
                frame_data = await read_frame(self._reader)
                _msg_type, correlation_id, payload = decode_frame(frame_data)
                future = self._pending.pop(correlation_id, None)
                if future is not None and not future.done():
                    logger.debug("Response %d received", correlation_id)
                    future.set_result(payload)
        except (asyncio.IncompleteReadError, ConnectionError):
            logger.info(
                "Disconnected by gateway %s (connection lost)",
                self._connected_gateway or "<unknown>",
            )
        except asyncio.CancelledError:
            cancelled = True
            return
        except TransportError as e:
            logger.warning("Protocol error in read loop: %s", e)
        finally:
            # Fail all remaining pending requests.
            err = ConnectionError("Connection lost")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(err)
            self._pending.clear()
            # On unexpected exit (not a cooperative close()), mark the writer
            # closed so the next invoke fails fast with ConnectionError instead
            # of waiting for the 30s request timeout against a zombie socket.
            if not cancelled and self._writer is not None:
                with contextlib.suppress(Exception):
                    self._writer.close()


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
