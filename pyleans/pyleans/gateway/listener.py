"""Gateway listener — TCP server that accepts client connections.

Runs inside a silo process. Each incoming request is dispatched to the
grain runtime and the result is sent back as a response frame.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

import orjson

from pyleans.errors import PyleansError, TransportError
from pyleans.gateway.protocol import MessageType, decode_frame, encode_frame, read_frame
from pyleans.identity import GrainId

if TYPE_CHECKING:
    from pyleans.server.runtime import GrainRuntime

logger = logging.getLogger(__name__)


class GatewayListener:
    """TCP server that accepts client connections and dispatches grain calls."""

    def __init__(self, runtime: GrainRuntime, host: str, port: int) -> None:
        self._runtime = runtime
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """The port the listener is bound to (useful when port=0)."""
        if self._server is not None and self._server.sockets:
            port: int = self._server.sockets[0].getsockname()[1]
            return port
        return self._port

    async def start(self) -> None:
        """Start accepting client connections."""
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        actual_port = self.port
        logger.info("Gateway listening on %s:%s", self._host, actual_port)

    async def stop(self) -> None:
        """Stop accepting connections and close existing ones."""
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("Gateway stopped")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single client connection (one task per client)."""
        peer = writer.get_extra_info("peername")
        logger.debug("Client connected: %s", peer)
        try:
            while True:
                try:
                    frame_data = await read_frame(reader)
                except asyncio.IncompleteReadError:
                    break  # client disconnected
                except TransportError as e:
                    logger.warning("Protocol error from %s: %s", peer, e)
                    break

                response_bytes = await self._dispatch(frame_data)
                writer.write(response_bytes)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            logger.debug("Client disconnected: %s", peer)

    async def _dispatch(self, frame_data: bytes) -> bytes:
        """Decode a request frame, invoke the grain, return a response frame."""
        try:
            msg_type, correlation_id, payload = decode_frame(frame_data)
        except TransportError as e:
            return encode_frame(MessageType.RESPONSE, 0, {"ok": False, "error": str(e)})

        if msg_type != MessageType.REQUEST:
            return encode_frame(
                MessageType.RESPONSE,
                correlation_id,
                {"ok": False, "error": f"Expected REQUEST, got {msg_type.name}"},
            )

        try:
            grain_type = payload["grain_type"]
            key = payload["key"]
            method = payload["method"]
            args = payload.get("args", [])
            kwargs = payload.get("kwargs", {})
        except KeyError as e:
            return encode_frame(
                MessageType.RESPONSE,
                correlation_id,
                {"ok": False, "error": f"Missing required field: {e}"},
            )

        grain_id = GrainId(grain_type=grain_type, key=key)
        try:
            result = await self._runtime.invoke(grain_id, method, args, kwargs)
            return encode_frame(
                MessageType.RESPONSE,
                correlation_id,
                {"ok": True, "result": _serialize_result(result)},
            )
        except PyleansError as e:
            return encode_frame(
                MessageType.RESPONSE,
                correlation_id,
                {"ok": False, "error": str(e)},
            )
        except Exception as e:
            logger.exception("Unexpected error dispatching grain call")
            return encode_frame(
                MessageType.RESPONSE,
                correlation_id,
                {"ok": False, "error": f"Internal error: {type(e).__name__}: {e}"},
            )


def _serialize_result(result: object) -> object:
    """Ensure the result is JSON-serializable.

    Primitive types pass through. For non-trivial types, attempt
    orjson round-trip to catch serialization errors early.
    """
    if result is None or isinstance(result, (bool, int, float, str)):
        return result
    # Round-trip through orjson to verify serializability
    orjson.dumps(result)
    return result
