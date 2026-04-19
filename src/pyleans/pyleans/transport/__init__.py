"""Pluggable cluster-transport port for pyleans.

Phase 2 silos communicate with each other exclusively through this
port. The default TCP adapter lives in task-02-08; this package ships
the port (ABCs, message types, error hierarchy, options) together with
the pure-codec layers (wire framing, handshake, error payload) that the
adapter composes.
"""

from pyleans.transport.cluster import (
    ConnectionCallback,
    DisconnectionCallback,
    IClusterTransport,
    ITransportFactory,
    MessageHandler,
)
from pyleans.transport.error_payload import (
    APPLICATION_ERROR_BASE,
    MAX_REASON_BYTES,
    ErrorCode,
    ErrorPayload,
    code_to_exception_class,
    decode_error_payload,
    encode_error_payload,
    exception_to_code,
)
from pyleans.transport.errors import (
    BackpressureError,
    HandshakeError,
    MessageTooLargeError,
    TransportClosedError,
    TransportConnectionError,
    TransportError,
    TransportTimeoutError,
)
from pyleans.transport.factory import TcpTransportFactory
from pyleans.transport.handshake import (
    MAGIC,
    MAX_HANDSHAKE_STRING,
    PROTOCOL_VERSION,
    ROLE_FLAG_GATEWAY_CAPABLE,
    Handshake,
    encode_handshake,
    read_handshake,
    validate_peer_handshake,
)
from pyleans.transport.messages import MessageDirection, MessageType, TransportMessage
from pyleans.transport.options import TransportOptions
from pyleans.transport.wire import (
    FRAME_OVERHEAD_AFTER_PREFIX,
    FRAME_PREFIX_SIZE,
    FrameHeader,
    decode_frame_prefix,
    encode_frame,
    read_frame,
)

__all__ = [
    "APPLICATION_ERROR_BASE",
    "FRAME_OVERHEAD_AFTER_PREFIX",
    "FRAME_PREFIX_SIZE",
    "MAGIC",
    "MAX_HANDSHAKE_STRING",
    "MAX_REASON_BYTES",
    "PROTOCOL_VERSION",
    "ROLE_FLAG_GATEWAY_CAPABLE",
    "BackpressureError",
    "ConnectionCallback",
    "DisconnectionCallback",
    "ErrorCode",
    "ErrorPayload",
    "FrameHeader",
    "Handshake",
    "HandshakeError",
    "IClusterTransport",
    "ITransportFactory",
    "MessageDirection",
    "MessageHandler",
    "MessageTooLargeError",
    "MessageType",
    "TcpTransportFactory",
    "TransportClosedError",
    "TransportConnectionError",
    "TransportError",
    "TransportMessage",
    "TransportOptions",
    "TransportTimeoutError",
    "code_to_exception_class",
    "decode_error_payload",
    "decode_frame_prefix",
    "encode_error_payload",
    "encode_frame",
    "encode_handshake",
    "exception_to_code",
    "read_frame",
    "read_handshake",
    "validate_peer_handshake",
]
