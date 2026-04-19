"""Transport-layer error hierarchy.

Callers in the grain runtime should catch these via the stdlib base
types where possible — :class:`TransportTimeoutError` is-a
:class:`TimeoutError` and :class:`TransportConnectionError` is-a
:class:`ConnectionError` — so the runtime stays decoupled from the
transport adapter identity.
"""

from __future__ import annotations


class TransportError(Exception):
    """Base class for every error originating in the transport layer."""


class HandshakeError(TransportError):
    """Peer rejected the handshake or sent an invalid handshake."""


class MessageTooLargeError(TransportError):
    """Frame exceeds ``max_message_size``; connection is terminated."""


class BackpressureError(TransportError):
    """Exceeded ``max_in_flight_requests`` while caller requested non-blocking."""


class TransportTimeoutError(TransportError, TimeoutError):
    """Request timed out waiting for a response."""


class TransportConnectionError(TransportError, ConnectionError):
    """Connection lost, refused, or unreachable."""


class TransportClosedError(TransportError):
    """Transport has been stopped; no further sends accepted."""
