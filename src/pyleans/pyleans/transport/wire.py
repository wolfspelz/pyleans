"""Wire-frame codec for the pyleans cluster transport.

Pure codec functions — no sockets, no connection state. The codec is
consumed by :mod:`pyleans.transport.connection` (task-02-06) to read
and write frames over ``asyncio.StreamReader`` / ``StreamWriter``,
and by the handshake (:mod:`pyleans.transport.handshake`) to gate
cluster membership at connection time.

Frame layout (big-endian throughout, see
:doc:`../../../docs/architecture/pyleans-transport` §4.3)::

    [4 bytes]  total frame length  (uint32, excludes these 4 bytes)
    [1 byte]   message type        (MessageType enum value)
    [8 bytes]  correlation id      (uint64)
    [4 bytes]  header length       (uint32)
    [H bytes]  header payload
    [B bytes]  body payload        (B = total - 13 - H)
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from pyleans.transport._io import readexactly_or_raise
from pyleans.transport.errors import MessageTooLargeError, TransportConnectionError
from pyleans.transport.messages import MessageType, TransportMessage

FRAME_PREFIX_SIZE = 4
FRAME_OVERHEAD_AFTER_PREFIX = 13

_UINT32_MAX = 2**32 - 1
_UINT64_MAX = 2**64 - 1

_FRAME_PREFIX = struct.Struct(">I")
_FRAME_FIXED = struct.Struct(">BQI")  # type (1) + correlation (8) + header_len (4)


@dataclass(frozen=True)
class FrameHeader:
    """Decoded view of a frame's fixed-size preamble.

    Exposed for callers (e.g. test harnesses and protocol tracing) that
    need to reason about a frame's shape without materialising the
    full payloads.
    """

    message_type: MessageType
    correlation_id: int
    header_length: int
    body_length: int


def encode_frame(message: TransportMessage, max_size: int) -> bytes:
    """Encode ``message`` as a single length-prefixed frame.

    Raises :class:`MessageTooLargeError` when the encoded frame exceeds
    ``max_size``. Raises :class:`ValueError` on field values outside
    their wire ranges (correlation id not a uint64, header/body too
    large to express in a uint32 field).
    """
    _validate_correlation_id(message.correlation_id)
    header_length = len(message.header)
    body_length = len(message.body)
    if header_length > _UINT32_MAX:
        raise ValueError(f"header length {header_length} exceeds uint32 max")
    total_length = FRAME_OVERHEAD_AFTER_PREFIX + header_length + body_length
    if total_length > _UINT32_MAX:
        raise ValueError(f"total frame length {total_length} exceeds uint32 max")
    full_size = FRAME_PREFIX_SIZE + total_length
    if full_size > max_size:
        raise MessageTooLargeError(f"frame size {full_size} exceeds max_size {max_size}")
    buffer = bytearray(full_size)
    _FRAME_PREFIX.pack_into(buffer, 0, total_length)
    _FRAME_FIXED.pack_into(
        buffer,
        FRAME_PREFIX_SIZE,
        int(message.message_type),
        message.correlation_id,
        header_length,
    )
    offset = FRAME_PREFIX_SIZE + FRAME_OVERHEAD_AFTER_PREFIX
    buffer[offset : offset + header_length] = message.header
    offset += header_length
    buffer[offset : offset + body_length] = message.body
    return bytes(buffer)


async def read_frame(reader: asyncio.StreamReader, max_size: int) -> TransportMessage:
    """Read exactly one frame from ``reader``.

    Raises:
        :class:`MessageTooLargeError`: declared total length exceeds
            ``max_size``. The body is *not* consumed.
        :class:`TransportConnectionError`: peer closed before the frame
            completed (wraps :class:`asyncio.IncompleteReadError`).
        :class:`ValueError`: malformed fields (unknown message type, or
            ``header_length > body region``).
    """
    prefix = await _readexactly(reader, FRAME_PREFIX_SIZE)
    (total_length,) = _FRAME_PREFIX.unpack(prefix)
    if total_length < FRAME_OVERHEAD_AFTER_PREFIX:
        raise ValueError(f"declared frame length {total_length} is shorter than overhead")
    full_size = FRAME_PREFIX_SIZE + total_length
    if full_size > max_size:
        raise MessageTooLargeError(f"frame size {full_size} exceeds max_size {max_size}")
    fixed = await _readexactly(reader, FRAME_OVERHEAD_AFTER_PREFIX)
    type_value, correlation_id, header_length = _FRAME_FIXED.unpack(fixed)
    payload_length = total_length - FRAME_OVERHEAD_AFTER_PREFIX
    if header_length > payload_length:
        raise ValueError(f"header length {header_length} exceeds payload region {payload_length}")
    body_length = payload_length - header_length
    try:
        message_type = MessageType(type_value)
    except ValueError as exc:
        raise ValueError(f"unknown message type {type_value:#x}") from exc
    header = await _readexactly(reader, header_length)
    body = await _readexactly(reader, body_length)
    return TransportMessage(
        message_type=message_type,
        correlation_id=correlation_id,
        header=header,
        body=body,
    )


def decode_frame_prefix(prefix: bytes) -> int:
    """Return the frame length encoded by a 4-byte prefix."""
    if len(prefix) != FRAME_PREFIX_SIZE:
        raise ValueError(f"prefix must be {FRAME_PREFIX_SIZE} bytes, got {len(prefix)}")
    length: int = _FRAME_PREFIX.unpack(prefix)[0]
    return length


async def _readexactly(reader: asyncio.StreamReader, count: int) -> bytes:
    return await readexactly_or_raise(reader, count, _truncation_to_connection_error)


def _truncation_to_connection_error(
    exc: asyncio.IncompleteReadError, expected: int
) -> TransportConnectionError:
    return TransportConnectionError(
        f"peer closed mid-frame: expected {expected} bytes, got {len(exc.partial)}"
    )


def _validate_correlation_id(correlation_id: int) -> None:
    if correlation_id < 0 or correlation_id > _UINT64_MAX:
        raise ValueError(f"correlation id {correlation_id} is not a uint64")
