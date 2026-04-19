"""Pluggable cluster-transport port for pyleans.

Phase 2 silos communicate with each other exclusively through this
port. The default TCP adapter lives in task-02-08; this package ships
only the contract (ABCs, message types, error hierarchy, options).
"""

from pyleans.transport.cluster import (
    ConnectionCallback,
    DisconnectionCallback,
    IClusterTransport,
    ITransportFactory,
    MessageHandler,
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
from pyleans.transport.messages import MessageDirection, MessageType, TransportMessage
from pyleans.transport.options import TransportOptions

__all__ = [
    "BackpressureError",
    "ConnectionCallback",
    "DisconnectionCallback",
    "HandshakeError",
    "IClusterTransport",
    "ITransportFactory",
    "MessageDirection",
    "MessageHandler",
    "MessageTooLargeError",
    "MessageType",
    "TransportClosedError",
    "TransportConnectionError",
    "TransportError",
    "TransportMessage",
    "TransportOptions",
    "TransportTimeoutError",
]
