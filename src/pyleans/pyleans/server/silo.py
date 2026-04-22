"""Silo — main entry point that wires the grain runtime + cluster stack.

``Silo.__init__`` composes every Phase 2 subsystem (cluster transport,
distributed directory, directory cache, failure detector, membership
agent, runtime, gateway) without performing any I/O. ``Silo.start``
then drives them through :class:`SiloLifecycle` in ascending stage
order so infrastructure comes up before calls are accepted, and
``Silo.stop`` takes them down in descending order so calls are
refused before infrastructure tears down.

Single-silo deployments are the ``N = 1`` special case: the ring
contains only the local silo, every grain resolves to local, and the
cluster transport has no peers to talk to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
import typing

from injector import Injector

from pyleans.cluster.directory_cache import DirectoryCache
from pyleans.cluster.distributed_directory import DistributedGrainDirectory
from pyleans.cluster.failure_detector import FailureDetector, FailureDetectorOptions
from pyleans.cluster.hash_ring import ConsistentHashRing
from pyleans.cluster.identity import ClusterId
from pyleans.cluster.membership_agent import MembershipAgent
from pyleans.cluster.placement import PlacementStrategy, PreferLocalPlacement
from pyleans.gateway.listener import GatewayListener
from pyleans.grain import _grain_registry, get_grain_type_name
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus
from pyleans.net import AsyncioNetwork, INetwork
from pyleans.providers.errors import SiloNotFoundError, TableStaleError
from pyleans.providers.membership import (
    MembershipProvider,
    MembershipSnapshot,
    with_replacements,
)
from pyleans.providers.storage import StorageProvider
from pyleans.providers.streaming import StreamProvider
from pyleans.reference import GrainFactory
from pyleans.serialization import JsonSerializer
from pyleans.server.container import create_injector
from pyleans.server.lifecycle import LifecycleStage, SiloLifecycle
from pyleans.server.providers.file_storage import FileStorageProvider
from pyleans.server.providers.memory_stream import InMemoryStreamProvider, StreamManager
from pyleans.server.providers.yaml_membership import YamlMembershipProvider
from pyleans.server.remote_invoke import GRAIN_CALL_HEADER
from pyleans.server.runtime import GrainRuntime
from pyleans.server.silo_management import SiloManagement
from pyleans.server.timer import TimerRegistry
from pyleans.transport.cluster import IClusterTransport
from pyleans.transport.messages import TransportMessage
from pyleans.transport.options import TransportOptions
from pyleans.transport.tcp.transport import TcpClusterTransport

logger = logging.getLogger(__name__)

_OCC_MAX_RETRIES = 5
_DEFAULT_IDLE_TIMEOUT = 900.0
_DEFAULT_PORT = 11111
_DEFAULT_GATEWAY_PORT = 30000
_DEFAULT_HOST = "localhost"
_DEFAULT_CLUSTER_ID = ClusterId("dev")
_PEER_CONNECT_TIMEOUT = 2.0


class Silo:
    """A pyleans silo — hosts grain activations and drives the cluster stack.

    Usage::

        silo = Silo(
            grains=[CounterGrain, PlayerGrain],
            storage_providers={"default": FileStorageProvider("./data")},
        )
        await silo.start()
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        grains: list[type],
        storage_providers: dict[str, StorageProvider] | None = None,
        membership_provider: MembershipProvider | None = None,
        stream_providers: dict[str, StreamProvider] | None = None,
        port: int = _DEFAULT_PORT,
        gateway_port: int = _DEFAULT_GATEWAY_PORT,
        host: str = _DEFAULT_HOST,
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
        *,
        cluster_id: ClusterId = _DEFAULT_CLUSTER_ID,
        placement: PlacementStrategy | None = None,
        failure_detector_options: FailureDetectorOptions | None = None,
        network: INetwork | None = None,
        epoch: int | None = None,
    ) -> None:
        self._grain_classes = grains
        self._host = host
        self._port = port
        self._gateway_port = gateway_port
        self._idle_timeout = idle_timeout
        self._cluster_id = cluster_id
        self._network = network or AsyncioNetwork()

        self._storage_providers = storage_providers or {
            "default": FileStorageProvider("./data/storage"),
        }
        self._membership_provider = membership_provider or YamlMembershipProvider(
            "./data/membership.yaml"
        )
        self._stream_providers = stream_providers or {
            "default": InMemoryStreamProvider(),
        }

        resolved_epoch = epoch if epoch is not None else int(time.time())
        self._silo_address = SiloAddress(host=self._host, port=self._port, epoch=resolved_epoch)
        self._silo_id = self._silo_address.silo_id

        self._silo_management = SiloManagement(silo=self)
        self._serializer = JsonSerializer()

        first_stream = next(iter(self._stream_providers.values()), None)
        stream_manager = StreamManager(first_stream) if first_stream else None

        self._placement: PlacementStrategy = placement or PreferLocalPlacement()
        self._fd_options = failure_detector_options or FailureDetectorOptions()

        self._transport: IClusterTransport = TcpClusterTransport(
            TransportOptions(network=self._network),
        )
        initial_ring = ConsistentHashRing([self._silo_address])
        self._directory = DistributedGrainDirectory(
            local_silo=self._silo_address,
            transport=self._transport,
            initial_ring=initial_ring,
            active_silos=[self._silo_address],
            local_activations_provider=self._local_activations,
        )
        self._cache = DirectoryCache(self._directory)

        self._runtime = GrainRuntime(
            storage_providers=self._storage_providers,
            serializer=self._serializer,
            idle_timeout=self._idle_timeout,
            directory=self._cache,
            local_silo=self._silo_address,
            placement_strategy=self._placement,
            transport=self._transport,
        )

        self._failure_detector = FailureDetector(
            local_silo=self._silo_address,
            cluster_id=self._cluster_id,
            options=self._fd_options,
            transport=self._transport,
            membership=self._membership_provider,
        )
        self._membership_agent = MembershipAgent(
            local_silo=self._silo_address,
            detector=self._failure_detector,
            transport=self._transport,
            membership=self._membership_provider,
        )

        self._grain_factory = GrainFactory(runtime=self._runtime)
        self._timer_registry = TimerRegistry(runtime=self._runtime)

        self._injector: Injector = create_injector(
            runtime=self._runtime,
            grain_factory=self._grain_factory,
            timer_registry=self._timer_registry,
            silo_management=self._silo_management,
            stream_manager=stream_manager,
        )
        self._runtime._grain_factory = self._injector.get

        self._gateway = GatewayListener(
            runtime=self._runtime,
            host=self._host,
            port=self._gateway_port,
            network=self._network,
        )

        self._directory.on_deactivate_notified(self._on_directory_deactivate)
        self._membership_agent.on_self_terminate(self._on_self_terminate)
        self._membership_agent.on_snapshot_received(self._on_snapshot_received)

        self._lifecycle = SiloLifecycle()
        self._register_lifecycle_participants()

        self._stop_event = asyncio.Event()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._started = False
        self._stopping = False
        self._atexit_cleanup: typing.Callable[[], None] | None = None

    # ---- Public API ----------------------------------------------------------

    @property
    def grain_factory(self) -> GrainFactory:
        """Access to grain factory (for co-hosted clients like FastAPI)."""
        return self._grain_factory

    @property
    def runtime(self) -> GrainRuntime:
        """Access to the grain runtime."""
        return self._runtime

    @property
    def timer_registry(self) -> TimerRegistry:
        """Access to the timer registry."""
        return self._timer_registry

    @property
    def gateway_port(self) -> int:
        """The actual gateway port (resolves port=0 after start)."""
        return self._gateway.port

    @property
    def address(self) -> SiloAddress:
        """The silo's own :class:`SiloAddress`."""
        return self._silo_address

    @property
    def cluster_transport(self) -> IClusterTransport:
        """The silo-to-silo cluster transport — exposed for integration harnesses."""
        return self._transport

    @property
    def directory(self) -> DistributedGrainDirectory:
        """The local-owner distributed directory partition."""
        return self._directory

    @property
    def cache(self) -> DirectoryCache:
        """The directory cache wrapping the distributed directory."""
        return self._cache

    @property
    def started(self) -> bool:
        """Whether the silo has been started."""
        return self._started

    async def start(self) -> None:
        """Start the silo and block until stopped."""
        await self._run_lifecycle_start()
        self._install_signal_handlers()
        self._install_atexit_handler()
        logger.info(
            "Silo started on %s:%s (gateway %s)",
            self._host,
            self._port,
            self._gateway.port,
        )
        await self._stop_event.wait()

    async def start_background(self) -> None:
        """Start the silo without blocking on the stop event."""
        await self._run_lifecycle_start()
        self._install_atexit_handler()
        logger.info(
            "Silo started on %s:%s (gateway %s, background)",
            self._host,
            self._port,
            self._gateway.port,
        )

    async def stop(self) -> None:
        """Graceful shutdown: lifecycle in reverse, then unregister."""
        if not self._started or self._stopping:
            return
        self._stopping = True

        logger.info("Silo shutting down...")
        await self._safe_transition_status(SiloStatus.SHUTTING_DOWN)

        await self._lifecycle.stop()

        await self._safe_unregister_from_membership()

        if self._atexit_cleanup is not None:
            import atexit

            atexit.unregister(self._atexit_cleanup)
            self._atexit_cleanup = None

        self._started = False
        self._stopping = False
        self._stop_event.set()

        logger.info("Silo stopped")

    # ---- Lifecycle setup -----------------------------------------------------

    def _register_lifecycle_participants(self) -> None:
        """Subscribe every subsystem to its lifecycle stage.

        Stage ordering rationale: runtime sits *above* transport in
        stage value so that during shutdown the reversal lets the
        runtime deactivate grains (and remote-unregister their
        directory entries over the still-alive transport) before the
        transport tears down. If we put runtime below transport,
        ``runtime.stop`` would run after ``transport.stop`` and grain
        unregister RPCs would fail silently, leaving stale directory
        entries for the rest of the cluster.
        """
        self._lifecycle.subscribe_hooks(
            LifecycleStage.RUNTIME_INITIALIZE,
            on_start=self._on_start_register,
            on_stop=self._no_op_stop,
            label="silo-register-in-membership",
        )
        self._lifecycle.subscribe_hooks(
            LifecycleStage.RUNTIME_CLUSTER,
            on_start=self._on_start_cluster,
            on_stop=self._on_stop_cluster,
            label="cluster-transport-and-directory",
        )
        self._lifecycle.subscribe_hooks(
            LifecycleStage.RUNTIME_GRAIN_SERVICES,
            on_start=self._on_start_runtime,
            on_stop=self._on_stop_runtime,
            label="grain-runtime",
        )
        self._lifecycle.subscribe_hooks(
            LifecycleStage.BECOME_ACTIVE,
            on_start=self._on_start_membership_agent,
            on_stop=self._on_stop_membership_agent,
            label="membership-agent",
        )
        self._lifecycle.subscribe_hooks(
            LifecycleStage.ACTIVE,
            on_start=self._on_start_gateway,
            on_stop=self._on_stop_gateway,
            label="gateway-listener",
        )

    async def _run_lifecycle_start(self) -> None:
        self._configure_default_logging()
        self._register_grain_classes()
        await self._lifecycle.start()
        self._started = True

    # ---- Lifecycle participants ----------------------------------------------

    async def _on_start_register(self, stage: int) -> None:
        del stage
        await self._register_in_membership()

    async def _on_start_runtime(self, stage: int) -> None:
        del stage
        await self._runtime.start()

    async def _on_stop_runtime(self, stage: int) -> None:
        del stage
        await self._runtime.stop()

    async def _on_start_cluster(self, stage: int) -> None:
        del stage
        await self._transport.start(
            self._silo_address,
            self._cluster_id,
            self._dispatch_transport_message,
        )
        snapshot = await self._read_membership_snapshot()
        active = self._active_silos(snapshot)
        await self._connect_to_peers(active)
        await self._directory.on_membership_changed(active)

    async def _on_stop_cluster(self, stage: int) -> None:
        del stage
        await self._transport.stop()

    async def _on_start_membership_agent(self, stage: int) -> None:
        del stage
        await self._membership_agent.start()

    async def _on_stop_membership_agent(self, stage: int) -> None:
        del stage
        await self._membership_agent.stop()

    async def _on_start_gateway(self, stage: int) -> None:
        del stage
        await self._gateway.start()

    async def _on_stop_gateway(self, stage: int) -> None:
        del stage
        await self._gateway.stop()

    async def _no_op_stop(self, stage: int) -> None:
        del stage

    # ---- Wiring helpers ------------------------------------------------------

    async def _dispatch_transport_message(
        self, source: SiloAddress, msg: TransportMessage
    ) -> TransportMessage | None:
        """Route an inbound transport frame to runtime / directory / agent."""
        if msg.header == GRAIN_CALL_HEADER:
            return await self._runtime.handle_grain_call(source, msg)
        directory_response = await self._directory.message_handler(source, msg)
        if directory_response is not None:
            return directory_response
        return await self._membership_agent.message_handler(source, msg)

    async def _local_activations(self) -> list:  # type: ignore[type-arg]
        """Return :class:`DirectoryEntry` for every live local activation.

        Supplied to :class:`DistributedGrainDirectory` so peers asking
        for their newly-owned arcs during a rebuild see every grain we
        currently host.
        """
        from pyleans.cluster.directory import DirectoryEntry

        entries: list[DirectoryEntry] = []
        for grain_id in list(self._runtime.activations.keys()):
            entries.append(
                DirectoryEntry(
                    grain_id=grain_id,
                    silo=self._silo_address,
                    activation_epoch=0,
                )
            )
        return entries

    async def _on_directory_deactivate(self, grain_id: typing.Any, activation_epoch: int) -> None:
        """Directory told us we lost a split-view race — stand the loser down."""
        del activation_epoch
        if grain_id in self._runtime.activations:
            logger.info("Directory deactivate: standing down activation of %s", grain_id)
            await self._runtime.deactivate_grain(grain_id)

    async def _on_self_terminate(self) -> None:
        """MembershipAgent saw our row flipped to DEAD — stop the silo."""
        logger.critical("Self-terminate requested by membership agent; stopping silo")
        loop = asyncio.get_running_loop()
        self._shutdown_task = loop.create_task(self.stop())

    async def _on_snapshot_received(self, snapshot: MembershipSnapshot) -> None:
        """Update the ring + drop cached routes whenever membership changes."""
        active = self._active_silos(snapshot)
        self._cache.invalidate_all()
        await self._directory.on_membership_changed(active)

    async def _read_membership_snapshot(self) -> MembershipSnapshot:
        return await self._membership_provider.read_all()

    def _active_silos(self, snapshot: MembershipSnapshot) -> list[SiloAddress]:
        """Return the cluster's active addresses, filtering out dead rows."""
        alive = {SiloStatus.JOINING, SiloStatus.ACTIVE}
        active = [s.address for s in snapshot.silos if s.status in alive]
        if self._silo_address not in active:
            active.append(self._silo_address)
        return active

    async def _connect_to_peers(self, active: list[SiloAddress]) -> None:
        """Best-effort outbound connect to every non-local active peer."""
        peers = [s for s in active if s != self._silo_address]
        if not peers:
            return
        results = await asyncio.gather(
            *(self._connect_peer(peer) for peer in peers),
            return_exceptions=True,
        )
        for peer, res in zip(peers, results, strict=True):
            if isinstance(res, BaseException):
                logger.warning("Peer connect to %s failed at startup: %r", peer.silo_id, res)

    async def _connect_peer(self, peer: SiloAddress) -> None:
        try:
            await asyncio.wait_for(
                self._transport.connect_to_silo(peer),
                timeout=_PEER_CONNECT_TIMEOUT,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Peer connect to %s raised %r", peer.silo_id, exc)

    # ---- Membership provider plumbing ---------------------------------------

    @staticmethod
    def _configure_default_logging() -> None:
        """Set up INFO logging if the application hasn't configured any handlers."""
        pyleans_logger = logging.getLogger("pyleans")
        if not pyleans_logger.handlers and not logging.root.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(name)s %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )

    def _register_grain_classes(self) -> None:
        """Ensure all grain classes are in the global registry."""
        for cls in self._grain_classes:
            grain_type = get_grain_type_name(cls)
            _grain_registry[grain_type] = cls

    async def _register_in_membership(self) -> None:
        """Register this silo in the membership table via the OCC primitive."""
        now = time.time()
        silo_info = SiloInfo(
            address=self._silo_address,
            status=SiloStatus.ACTIVE,
            last_heartbeat=now,
            start_time=now,
            cluster_id=self._cluster_id.value,
            gateway_port=self._gateway_port,
            i_am_alive=now,
        )
        await self._occ_write(silo_info)
        logger.info("Registered in membership table as %s", self._silo_id)

    async def _safe_transition_status(self, status: SiloStatus) -> None:
        try:
            await self._transition_status(status)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Status transition to %s failed: %r", status.value, exc)

    async def _safe_unregister_from_membership(self) -> None:
        try:
            await self._unregister_from_membership()
            logger.info("Unregistered from membership table")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Unregister from membership failed: %r", exc)

    async def _transition_status(self, status: SiloStatus) -> None:
        """Read-modify-write the current row to transition its status."""
        for _ in range(_OCC_MAX_RETRIES):
            current = await self._membership_provider.read_silo(self._silo_id)
            if current is None:
                raise SiloNotFoundError(f"Silo {self._silo_id!r} not found in membership table")
            updated = with_replacements(current, status=status)
            try:
                await self._membership_provider.try_update_silo(updated)
                return
            except TableStaleError:
                continue
        raise TableStaleError(
            f"status transition for {self._silo_id!r} failed after {_OCC_MAX_RETRIES} retries"
        )

    async def _unregister_from_membership(self) -> None:
        """Physically remove this silo's row via the OCC delete primitive."""
        for _ in range(_OCC_MAX_RETRIES):
            current = await self._membership_provider.read_silo(self._silo_id)
            if current is None:
                return
            try:
                await self._membership_provider.try_delete_silo(current)
                return
            except TableStaleError:
                continue
        raise TableStaleError(
            f"unregister for {self._silo_id!r} failed after {_OCC_MAX_RETRIES} retries"
        )

    async def _occ_write(self, silo_info: SiloInfo) -> None:
        """Create-or-update the local row, retrying on etag races."""
        for _ in range(_OCC_MAX_RETRIES):
            existing = await self._membership_provider.read_silo(self._silo_id)
            candidate = with_replacements(
                silo_info, etag=existing.etag if existing is not None else None
            )
            try:
                await self._membership_provider.try_update_silo(candidate)
                return
            except TableStaleError:
                continue
        raise TableStaleError(
            f"register for {self._silo_id!r} failed after {_OCC_MAX_RETRIES} retries"
        )

    def _install_signal_handlers(self) -> None:
        """Install handlers for all catchable termination signals."""
        loop = asyncio.get_running_loop()

        def _handle_signal() -> None:
            logger.info("Received shutdown signal")
            self._shutdown_task = loop.create_task(self.stop())

        try:
            loop.add_signal_handler(signal.SIGINT, _handle_signal)
            loop.add_signal_handler(signal.SIGTERM, _handle_signal)
        except NotImplementedError:
            # Windows ProactorEventLoop doesn't support add_signal_handler.
            def _win_handler(signum: int, _frame: object) -> None:
                logger.info("Received shutdown signal (%s)", signal.Signals(signum).name)
                loop.call_soon_threadsafe(lambda: loop.create_task(self.stop()))

            signal.signal(signal.SIGINT, _win_handler)

    def _install_atexit_handler(self) -> None:
        """Register atexit cleanup to unregister from membership on unexpected exit."""
        import atexit

        def _cleanup() -> None:
            if not self._started:
                return
            try:
                loop = asyncio.new_event_loop()
                with contextlib.suppress(Exception):
                    loop.run_until_complete(self._unregister_from_membership())
                loop.close()
                logger.info("atexit: unregistered from membership table")
            except Exception:  # pylint: disable=broad-except
                logger.warning("atexit: failed to unregister from membership", exc_info=True)

        atexit.register(_cleanup)
        self._atexit_cleanup = _cleanup
