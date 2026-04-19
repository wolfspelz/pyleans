"""Default :class:`ITransportFactory` implementation.

:class:`TcpTransportFactory` wraps :class:`TcpClusterTransport`
construction behind the :class:`ITransportFactory` port so silo
assembly (task-02-17) can swap in alternate transports (MQTT,
WebSocket, test doubles) without re-plumbing wiring code.
"""

from __future__ import annotations

from pyleans.transport.cluster import IClusterTransport, ITransportFactory
from pyleans.transport.options import TransportOptions
from pyleans.transport.tcp.transport import TcpClusterTransport


class TcpTransportFactory(ITransportFactory):
    """Produces :class:`TcpClusterTransport` instances from options."""

    def create_cluster_transport(self, options: TransportOptions) -> IClusterTransport:
        return TcpClusterTransport(options)
