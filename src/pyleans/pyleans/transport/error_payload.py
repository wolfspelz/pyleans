"""Structured error payload for ``MessageType.ERROR`` responses.

Wire layout (big-endian)::

    [4 bytes]  error code     uint32
    [4 bytes]  reason length  uint32 (<= MAX_REASON_BYTES)
    [N bytes]  reason         UTF-8, truncated to MAX_REASON_BYTES

Error-code mapping lives in :class:`ErrorCode`: codes 1-99 map onto the
transport error hierarchy from task-02-04, codes 100+ denote application
errors (the reason is the stringified exception; task-02-16 decides
whether to reconstruct).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from pyleans.transport.errors import (
    HandshakeError,
    MessageTooLargeError,
    TransportConnectionError,
    TransportError,
    TransportTimeoutError,
)

MAX_REASON_BYTES = 1024
APPLICATION_ERROR_BASE = 100

_HEADER_STRUCT = struct.Struct(">II")


class ErrorCode(IntEnum):
    """Numeric codes that identify the error class carried on the wire."""

    TRANSPORT_ERROR = 1
    HANDSHAKE_ERROR = 2
    MESSAGE_TOO_LARGE_ERROR = 3
    TRANSPORT_TIMEOUT_ERROR = 4
    TRANSPORT_CONNECTION_ERROR = 5
    APPLICATION_ERROR = APPLICATION_ERROR_BASE


_CODE_TO_EXCEPTION: dict[int, type[Exception]] = {
    ErrorCode.TRANSPORT_ERROR: TransportError,
    ErrorCode.HANDSHAKE_ERROR: HandshakeError,
    ErrorCode.MESSAGE_TOO_LARGE_ERROR: MessageTooLargeError,
    ErrorCode.TRANSPORT_TIMEOUT_ERROR: TransportTimeoutError,
    ErrorCode.TRANSPORT_CONNECTION_ERROR: TransportConnectionError,
}


@dataclass(frozen=True)
class ErrorPayload:
    """Decoded structured error body."""

    code: int
    reason: str

    @property
    def is_application_error(self) -> bool:
        return self.code >= APPLICATION_ERROR_BASE


def encode_error_payload(payload: ErrorPayload) -> bytes:
    """Serialise ``payload`` to the wire error body.

    ``reason`` is truncated to :data:`MAX_REASON_BYTES` on UTF-8 byte
    length before encoding; ``code`` must fit in a uint32.
    """
    if not 0 <= payload.code <= 0xFFFFFFFF:
        raise ValueError(f"error code {payload.code} is not a uint32")
    reason_bytes = payload.reason.encode("utf-8")[:MAX_REASON_BYTES]
    return _HEADER_STRUCT.pack(payload.code, len(reason_bytes)) + reason_bytes


def decode_error_payload(body: bytes) -> ErrorPayload:
    """Deserialise ``body`` produced by :func:`encode_error_payload`."""
    if len(body) < _HEADER_STRUCT.size:
        raise ValueError(
            f"error payload must be at least {_HEADER_STRUCT.size} bytes, got {len(body)}"
        )
    code, reason_length = _HEADER_STRUCT.unpack_from(body, 0)
    remaining = len(body) - _HEADER_STRUCT.size
    if reason_length > remaining:
        raise ValueError(
            f"declared reason length {reason_length} exceeds remaining {remaining} bytes"
        )
    if reason_length > MAX_REASON_BYTES:
        raise ValueError(
            f"declared reason length {reason_length} exceeds MAX_REASON_BYTES {MAX_REASON_BYTES}"
        )
    reason_bytes = body[_HEADER_STRUCT.size : _HEADER_STRUCT.size + reason_length]
    try:
        reason = reason_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("error reason is not valid UTF-8") from exc
    return ErrorPayload(code=code, reason=reason)


def exception_to_code(exc: BaseException) -> int:
    """Return the :class:`ErrorCode` for ``exc`` or :data:`APPLICATION_ERROR_BASE`."""
    if isinstance(exc, HandshakeError):
        return ErrorCode.HANDSHAKE_ERROR
    if isinstance(exc, MessageTooLargeError):
        return ErrorCode.MESSAGE_TOO_LARGE_ERROR
    if isinstance(exc, TransportTimeoutError):
        return ErrorCode.TRANSPORT_TIMEOUT_ERROR
    if isinstance(exc, TransportConnectionError):
        return ErrorCode.TRANSPORT_CONNECTION_ERROR
    if isinstance(exc, TransportError):
        return ErrorCode.TRANSPORT_ERROR
    return APPLICATION_ERROR_BASE


def code_to_exception_class(code: int) -> type[Exception]:
    """Return the transport exception class for ``code``, or ``TransportError`` otherwise."""
    if code >= APPLICATION_ERROR_BASE:
        return TransportError
    return _CODE_TO_EXCEPTION.get(code, TransportError)
