"""Concrete :class:`IClusterTransport` adapter over TCP.

:class:`TcpClusterTransport` composes the task-02-05 wire codec,
task-02-06 :class:`SiloConnection`, task-02-07
:class:`SiloConnectionManager`, and the task-01-15 :class:`INetwork`
port. It is deliberately thin: all reliability and multiplexing logic
lives in the lower layers, and this class only supplies lifecycle,
listener wiring, and the port-level contract.

See :doc:`../../../../docs/adr/adr-cluster-transport` and
:doc:`../../../../docs/architecture/pyleans-transport` §4 for design
context, and
:doc:`../../../../docs/tasks/task-02-08-tcp-cluster-transport` for the
task spec.
"""

from __future__ import annotations

import logging
from typing import Literal

from pyleans.cluster import ClusterId
from pyleans.identity import SiloAddress
from pyleans.net import NetworkServer
from pyleans.transport.cluster import (
    ConnectionCallback,
    DisconnectionCallback,
    IClusterTransport,
    MessageHandler,
)
from pyleans.transport.errors import TransportClosedError
from pyleans.transport.options import TransportOptions
from pyleans.transport.tcp.manager import SiloConnectionManager

logger = logging.getLogger(__name__)

_State = Literal["new", "running", "stopped"]


class TcpClusterTransport(IClusterTransport):
    """TCP mesh implementation of :class:`IClusterTransport`."""

    def __init__(self, options: TransportOptions) -> None:
        self._options = options
        self._state: _State = "new"
        self._local_silo: SiloAddress | None = None
        self._cluster_id: ClusterId | None = None
        self._manager: SiloConnectionManager | None = None
        self._server: NetworkServer | None = None
        self._on_established: list[ConnectionCallback] = []
        self._on_lost: list[DisconnectionCallback] = []

    @property
    def local_silo(self) -> SiloAddress:
        if self._local_silo is None:
            raise TransportClosedError("transport not started")
        return self._local_silo

    async def start(
        self,
        local_silo: SiloAddress,
        cluster_id: ClusterId,
        message_handler: MessageHandler,
    ) -> None:
        """Bind the listener, install the manager, and begin accepting peers."""
        if self._state == "running":
            raise RuntimeError("transport already started")
        if self._state == "stopped":
            raise RuntimeError("transport already stopped; create a new instance")
        self._local_silo = local_silo
        self._cluster_id = cluster_id
        self._manager = SiloConnectionManager(
            local_silo, cluster_id, self._options, message_handler
        )
        self._manager.on_connection_established(self._dispatch_established)
        self._manager.on_connection_lost(self._dispatch_lost)
        self._server = await self._options.network.start_server(
            self._manager.accept_inbound,
            host=local_silo.host,
            port=local_silo.port,
            ssl=self._options.ssl_context,
        )
        bound_port = self._server.sockets[0].getsockname()[1]
        if bound_port != local_silo.port:
            # Resolve ephemeral (port=0) requests to the actual bound port so
            # the handshake advertises a silo_id peers can dial back.
            self._local_silo = SiloAddress(
                host=local_silo.host, port=bound_port, epoch=local_silo.epoch
            )
            self._manager._local_silo = self._local_silo  # pylint: disable=protected-access
        self._state = "running"
        logger.info(
            "TcpClusterTransport listening: silo=%s cluster=%s",
            self._local_silo,
            cluster_id.value,
        )

    async def stop(self) -> None:
        """Close the listener, then tear down every outbound connection."""
        if self._state != "running":
            return
        self._state = "stopped"
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._manager is not None:
            await self._manager.stop()
        logger.info("TcpClusterTransport stopped: silo=%s", self._local_silo)

    async def connect_to_silo(self, silo: SiloAddress) -> None:
        await self._require_manager().connect(silo)

    async def disconnect_from_silo(self, silo: SiloAddress) -> None:
        await self._require_manager().disconnect(silo)

    async def send_request(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        self._reject_self_send(target)
        conn = await self._require_manager().connect(target)
        return await conn.send_request(header, body, timeout)

    async def send_one_way(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
    ) -> None:
        self._reject_self_send(target)
        conn = await self._require_manager().connect(target)
        await conn.send_one_way(header, body)

    async def send_ping(self, target: SiloAddress, timeout: float = 10.0) -> float:
        self._reject_self_send(target)
        conn = await self._require_manager().connect(target)
        return await conn.send_ping(timeout)

    def is_connected_to(self, silo: SiloAddress) -> bool:
        if self._manager is None:
            return False
        return self._manager.get_connection(silo) is not None

    def get_connected_silos(self) -> list[SiloAddress]:
        if self._manager is None:
            return []
        return self._manager.get_connected_silos()

    def on_connection_established(self, callback: ConnectionCallback) -> None:
        self._on_established.append(callback)

    def on_connection_lost(self, callback: DisconnectionCallback) -> None:
        self._on_lost.append(callback)

    def _require_manager(self) -> SiloConnectionManager:
        if self._state != "running" or self._manager is None:
            raise TransportClosedError(f"transport not running (state={self._state!r})")
        return self._manager

    def _reject_self_send(self, target: SiloAddress) -> None:
        if self._local_silo is not None and target == self._local_silo:
            raise ValueError(
                f"refusing to route {target} through the transport — "
                "local calls must bypass the cluster transport"
            )

    async def _dispatch_established(self, silo: SiloAddress) -> None:
        for callback in list(self._on_established):
            try:
                await callback(silo)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("on_connection_established observer raised")

    async def _dispatch_lost(self, silo: SiloAddress, cause: Exception | None) -> None:
        for callback in list(self._on_lost):
            try:
                await callback(silo, cause)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("on_connection_lost observer raised")
