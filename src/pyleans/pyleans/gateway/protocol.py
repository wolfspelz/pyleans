"""Binary frame encoding for the pyleans gateway protocol.

Wire format (from pyleans-transport.md):
    [4 bytes: total frame length (uint32 big-endian, excludes these 4 bytes)]
    [1 byte:  message type]
    [8 bytes: correlation ID (uint64 big-endian)]
    [remaining bytes: payload]

Phase 1 uses JSON payloads. Phase 2 may switch to a more efficient format.
"""

import asyncio
import enum
import struct
from typing import Any

import orjson

from pyleans.errors import TransportError

HEADER_STRUCT = struct.Struct("!IbQ")  # frame_len (4), msg_type (1), corr_id (8)
HEADER_SIZE = HEADER_STRUCT.size  # 13 bytes
FRAME_PREFIX_SIZE = 4  # the 4-byte length prefix itself
MIN_FRAME_SIZE = 1 + 8  # message type + correlation ID (no payload)
MAX_FRAME_SIZE = 16 * 1024 * 1024  # 16 MB


class MessageType(enum.IntEnum):
    """Gateway message types."""

    REQUEST = 0x01
    RESPONSE = 0x02


def encode_frame(msg_type: MessageType, correlation_id: int, payload: dict[str, Any]) -> bytes:
    """Encode a gateway frame as bytes ready to send on the wire.

    Returns the complete frame including the 4-byte length prefix.
    """
    body = orjson.dumps(payload)
    frame_len = MIN_FRAME_SIZE + len(body)
    if frame_len > MAX_FRAME_SIZE:
        raise TransportError(f"Frame size {frame_len} exceeds maximum {MAX_FRAME_SIZE}")
    header = HEADER_STRUCT.pack(frame_len, msg_type, correlation_id)
    return header + body


def decode_frame(data: bytes) -> tuple[MessageType, int, dict[str, Any]]:
    """Decode a complete frame (including the 4-byte length prefix).

    Returns (message_type, correlation_id, payload_dict).
    """
    if len(data) < HEADER_SIZE:
        raise TransportError(f"Frame too short: {len(data)} bytes")
    _frame_len, msg_type_raw, correlation_id = HEADER_STRUCT.unpack_from(data)
    try:
        msg_type = MessageType(msg_type_raw)
    except ValueError as e:
        raise TransportError(f"Unknown message type: {msg_type_raw:#x}") from e
    payload_bytes = data[HEADER_SIZE:]
    try:
        payload: dict[str, Any] = orjson.loads(payload_bytes)
    except orjson.JSONDecodeError as e:
        raise TransportError(f"Invalid JSON payload: {e}") from e
    return msg_type, correlation_id, payload


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one complete frame from the stream.

    Returns the full frame bytes (including the 4-byte length prefix).
    Raises ``TransportError`` on protocol violations and
    ``ConnectionError`` when the peer disconnects.
    """
    length_bytes = await reader.readexactly(FRAME_PREFIX_SIZE)
    (frame_len,) = struct.unpack("!I", length_bytes)
    if frame_len < MIN_FRAME_SIZE:
        raise TransportError(f"Frame length too small: {frame_len}")
    if frame_len > MAX_FRAME_SIZE:
        raise TransportError(f"Frame length {frame_len} exceeds maximum {MAX_FRAME_SIZE}")
    body = await reader.readexactly(frame_len)
    return length_bytes + body
