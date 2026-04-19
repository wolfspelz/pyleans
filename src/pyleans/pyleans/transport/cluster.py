"""Port definitions for silo-to-silo cluster transport.

Defines the :class:`IClusterTransport` ABC, callback type aliases, and
the :class:`ITransportFactory` factory port. The concrete TCP adapter
lives in task-02-08; this module ships only the contract.

See :doc:`../../../docs/adr/adr-cluster-transport` for the pluggability
rationale and :doc:`../../../docs/adr/adr-single-activation-cluster`
for the role of the transport in enforcing the single-activation
contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from pyleans.cluster.identity import ClusterId
from pyleans.identity import SiloAddress
from pyleans.transport.messages import TransportMessage
from pyleans.transport.options import TransportOptions

MessageHandler = Callable[
    [SiloAddress, TransportMessage],
    Awaitable[TransportMessage | None],
]
"""Callback fired for every inbound :class:`TransportMessage`.

Contract (enforced by the adapter, not the handler):

* For :attr:`MessageType.REQUEST` input the handler **must** return a
  :class:`TransportMessage` with ``message_type=RESPONSE`` and the
  matching ``correlation_id``. If the handler raises, the adapter wraps
  the exception into a :attr:`MessageType.ERROR` response.
* For :attr:`MessageType.ONE_WAY` input the handler returns ``None``.
* The handler never sees ``PING`` / ``PONG`` / ``ERROR`` — those are
  resolved inside the adapter.
"""

ConnectionCallback = Callable[[SiloAddress], Awaitable[None]]
"""Invoked when a peer connection becomes live."""

DisconnectionCallback = Callable[[SiloAddress, Exception | None], Awaitable[None]]
"""Invoked when a peer connection drops.

The second argument is the cause when available (or ``None`` for a
clean, requested disconnect).
"""


class IClusterTransport(ABC):
    """Port for silo-to-silo communication.

    Contract guarantees every adapter must honour:

    * ``send_request`` / ``send_one_way`` / ``send_ping`` never return
      before the message has been accepted by the adapter.
    * In-order per-peer delivery: messages sent to the same silo arrive
      in send order.
    * Fail-fast on disconnect: every outstanding ``send_request`` future
      faults with :class:`TransportConnectionError` the moment the
      connection is classified as lost. The adapter must not silently
      retry.
    * Reconnection is the adapter's responsibility for as long as the
      peer remains in the active silo set.

    Lifecycle::

        tx = factory.create_cluster_transport(options)
        await tx.start(local_silo, cluster_id, handler)
        # ... use send_request / send_one_way ...
        await tx.stop()
    """

    @abstractmethod
    async def start(
        self,
        local_silo: SiloAddress,
        cluster_id: ClusterId,
        message_handler: MessageHandler,
    ) -> None:
        """Bind the local listener, install the handler, and allow peer connections."""

    @abstractmethod
    async def stop(self) -> None:
        """Terminate all connections and release transport resources."""

    @abstractmethod
    async def connect_to_silo(self, silo: SiloAddress) -> None:
        """Establish an outbound connection to ``silo`` if one is not already live."""

    @abstractmethod
    async def disconnect_from_silo(self, silo: SiloAddress) -> None:
        """Tear down the connection to ``silo`` cleanly."""

    @abstractmethod
    async def send_request(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        """Send a request and return the peer's ``(header, body)`` response."""

    @abstractmethod
    async def send_one_way(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
    ) -> None:
        """Send a message that expects no response."""

    @abstractmethod
    async def send_ping(self, target: SiloAddress, timeout: float = 10.0) -> float:
        """Send a PING and return the round-trip time in seconds."""

    @abstractmethod
    def is_connected_to(self, silo: SiloAddress) -> bool:
        """Return ``True`` if a live connection to ``silo`` exists."""

    @abstractmethod
    def get_connected_silos(self) -> list[SiloAddress]:
        """Return a snapshot of peers currently connected."""

    @property
    @abstractmethod
    def local_silo(self) -> SiloAddress:
        """Return the local silo address (set via :meth:`start`)."""

    @abstractmethod
    def on_connection_established(self, callback: ConnectionCallback) -> None:
        """Register a callback fired whenever a peer connection becomes live."""

    @abstractmethod
    def on_connection_lost(self, callback: DisconnectionCallback) -> None:
        """Register a callback fired whenever a peer connection drops."""


class ITransportFactory(ABC):
    """Creates :class:`IClusterTransport` instances from options."""

    @abstractmethod
    def create_cluster_transport(self, options: TransportOptions) -> IClusterTransport:
        """Return a new, not-yet-started transport configured from ``options``."""
