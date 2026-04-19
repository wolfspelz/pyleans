"""Transport-layer configuration options.

Defaults match :doc:`../../../docs/architecture/pyleans-transport` §6.1.
The ``network`` field carries the :class:`INetwork` port so the TCP
transport adapter (task-02-08) and its tests share one abstraction —
production wires :class:`AsyncioNetwork`, tests wire
:class:`InMemoryNetwork`.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass, field

from pyleans.net import AsyncioNetwork, INetwork

DEFAULT_MAX_MESSAGE_SIZE = 16 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_MAX_IN_FLIGHT_REQUESTS = 1000
DEFAULT_KEEPALIVE_INTERVAL = 30.0
DEFAULT_KEEPALIVE_TIMEOUT = 10.0
DEFAULT_RECONNECT_BASE_DELAY = 0.1
DEFAULT_RECONNECT_MAX_DELAY = 30.0
DEFAULT_RECONNECT_JITTER_FRACTION = 0.3
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_HANDSHAKE_TIMEOUT = 5.0


@dataclass
class TransportOptions:
    """Configuration for :class:`IClusterTransport` implementations."""

    max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE
    default_request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_in_flight_requests: int = DEFAULT_MAX_IN_FLIGHT_REQUESTS
    keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL
    keepalive_timeout: float = DEFAULT_KEEPALIVE_TIMEOUT
    reconnect_base_delay: float = DEFAULT_RECONNECT_BASE_DELAY
    reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY
    reconnect_jitter_fraction: float = DEFAULT_RECONNECT_JITTER_FRACTION
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    ssl_context: ssl.SSLContext | None = None
    network: INetwork = field(default_factory=AsyncioNetwork)
