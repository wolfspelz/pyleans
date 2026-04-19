"""Transport-layer message types.

:class:`TransportMessage` is the opaque unit of exchange between silos.
The header and body are bytes produced by the layer above — the
transport never inspects their contents. This keeps the transport
schema-agnostic and lets protocol evolution happen in the grain runtime
without touching networking code.

See :doc:`../../../docs/architecture/pyleans-transport` §4 and §6 for
the wire-protocol framing and the message-direction semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class MessageType(IntEnum):
    """Wire-level message discriminator.

    Values match the codepoints in
    :doc:`../../../docs/architecture/pyleans-transport` §4.3 so the
    wire codec in task-02-05 can reuse this enum directly without
    defining a second mapping.
    """

    REQUEST = 0x01
    RESPONSE = 0x02
    ONE_WAY = 0x03
    PING = 0x04
    PONG = 0x05
    ERROR = 0x06


class MessageDirection(IntEnum):
    """Per-message direction hint used by logging and metrics.

    Not serialised on the wire — the transport derives the direction
    from which side originated the message.
    """

    OUTBOUND = 1
    INBOUND = 2


@dataclass(frozen=True)
class TransportMessage:
    """Opaque message exchanged across the transport.

    ``header`` and ``body`` are bytes supplied by the layer above; the
    transport forwards them without inspection. ``correlation_id`` is a
    64-bit unsigned identifier the sender uses to pair a response with
    its request. It is 0 for :attr:`MessageType.ONE_WAY`. For
    :attr:`MessageType.PING` / :attr:`MessageType.PONG`, the sender
    allocates a non-zero id so the PONG can be matched to its PING
    (keepalive RTT measurement); all other message types always carry a
    non-zero id.
    """

    message_type: MessageType
    correlation_id: int
    header: bytes
    body: bytes
