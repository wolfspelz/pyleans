"""TCP cluster-transport adapter — connection-level building blocks.

Task-02-06 ships :class:`SiloConnection` (one multiplexed TCP pair
between two silos) and its :class:`PendingRequests` correlation table.
Task-02-07 adds per-peer pooling, task-02-08 assembles the full
:class:`IClusterTransport` adapter.
"""

from pyleans.transport.tcp.connection import SiloConnection
from pyleans.transport.tcp.manager import SiloConnectionManager
from pyleans.transport.tcp.pending import PendingRequests
from pyleans.transport.tcp.transport import TcpClusterTransport

__all__ = [
    "PendingRequests",
    "SiloConnection",
    "SiloConnectionManager",
    "TcpClusterTransport",
]
