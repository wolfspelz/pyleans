"""Silo — main entry point that wires together the grain runtime."""

import asyncio
import contextlib
import logging
import signal
import time

from dependency_injector import providers as di_providers

from pyleans.gateway.listener import GatewayListener
from pyleans.grain import _grain_registry, get_grain_type_name
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus
from pyleans.providers.membership import MembershipProvider
from pyleans.providers.storage import StorageProvider
from pyleans.providers.streaming import StreamProvider
from pyleans.reference import GrainFactory
from pyleans.serialization import JsonSerializer
from pyleans.server.container import PyleansContainer
from pyleans.server.providers.file_storage import FileStorageProvider
from pyleans.server.providers.memory_stream import InMemoryStreamProvider
from pyleans.server.providers.yaml_membership import YamlMembershipProvider
from pyleans.server.runtime import GrainRuntime
from pyleans.server.silo_management import SiloManagement
from pyleans.server.timer import TimerRegistry

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30.0
_DEFAULT_IDLE_TIMEOUT = 900.0
_DEFAULT_PORT = 11111
_DEFAULT_GATEWAY_PORT = 30000
_DEFAULT_HOST = "localhost"


class Silo:
    """A pyleans silo — hosts grain activations and manages the runtime.

    Usage::

        silo = Silo(
            grains=[CounterGrain, PlayerGrain],
            storage_providers={"default": FileStorageProvider("./data")},
        )
        await silo.start()
    """

    def __init__(
        self,
        grains: list[type],
        storage_providers: dict[str, StorageProvider] | None = None,
        membership_provider: MembershipProvider | None = None,
        stream_providers: dict[str, StreamProvider] | None = None,
        port: int = _DEFAULT_PORT,
        gateway_port: int = _DEFAULT_GATEWAY_PORT,
        host: str = _DEFAULT_HOST,
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self._grain_classes = grains
        self._host = host
        self._port = port
        self._gateway_port = gateway_port
        self._idle_timeout = idle_timeout

        self._storage_providers = storage_providers or {
            "default": FileStorageProvider("./pyleans-data/storage"),
        }
        self._membership_provider = membership_provider or YamlMembershipProvider(
            "./pyleans-data/membership.yaml"
        )
        self._stream_providers = stream_providers or {
            "default": InMemoryStreamProvider(),
        }

        epoch = int(time.time())
        self._silo_address = SiloAddress(host=self._host, port=self._port, epoch=epoch)
        self._silo_id = self._silo_address.encoded

        self._silo_management = SiloManagement(silo=self)
        self._serializer = JsonSerializer()
        self._runtime = GrainRuntime(
            storage_providers=self._storage_providers,
            serializer=self._serializer,
            idle_timeout=self._idle_timeout,
        )
        self._grain_factory = GrainFactory(runtime=self._runtime)
        self._timer_registry = TimerRegistry(runtime=self._runtime)

        # DI container — override with actual instances
        self._container = PyleansContainer()
        self._container.runtime.override(di_providers.Object(self._runtime))
        self._container.grain_factory.override(di_providers.Object(self._grain_factory))
        self._container.timer_registry.override(di_providers.Object(self._timer_registry))
        self._container.silo_management.override(di_providers.Object(self._silo_management))

        self._gateway = GatewayListener(
            runtime=self._runtime, host=self._host, port=self._gateway_port
        )

        self._stop_event = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._started = False

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
    def started(self) -> bool:
        """Whether the silo has been started."""
        return self._started

    async def start(self) -> None:
        """Start the silo and block until stopped.

        Registers grain classes, initializes providers, starts the runtime
        idle collector and heartbeat, then waits for the stop event.
        """
        self._register_grain_classes()
        self._wire_container()
        await self._register_in_membership()
        await self._runtime.start()
        await self._gateway.start()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._started = True
        self._install_signal_handlers()

        logger.info(
            "Silo started on %s:%s (gateway %s)",
            self._host,
            self._port,
            self._gateway.port,
        )

        await self._stop_event.wait()

    async def start_background(self) -> None:
        """Start the silo without blocking.

        Same as start() but returns immediately instead of waiting for
        the stop event. Useful for embedding a silo in an application
        (e.g. FastAPI) or in tests.
        """
        self._register_grain_classes()
        self._wire_container()
        await self._register_in_membership()
        await self._runtime.start()
        await self._gateway.start()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._started = True

        logger.info(
            "Silo started on %s:%s (gateway %s, background)",
            self._host,
            self._port,
            self._gateway.port,
        )

    async def stop(self) -> None:
        """Graceful shutdown: deactivate grains, unregister, cancel tasks."""
        if not self._started:
            return

        logger.info("Silo shutting down...")

        await self._membership_provider.update_status(self._silo_id, SiloStatus.SHUTTING_DOWN)

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

        await self._gateway.stop()
        await self._runtime.stop()

        await self._membership_provider.unregister_silo(self._silo_id)

        self._started = False
        self._stop_event.set()

        logger.info("Silo stopped")

    def _register_grain_classes(self) -> None:
        """Ensure all grain classes are in the global registry."""
        for cls in self._grain_classes:
            grain_type = get_grain_type_name(cls)
            _grain_registry[grain_type] = cls

    def _wire_container(self) -> None:
        """Wire the DI container so @inject works in grain modules."""
        import sys

        modules_to_wire = []
        for cls in self._grain_classes:
            mod = sys.modules.get(cls.__module__)
            if mod is not None and mod not in modules_to_wire:
                modules_to_wire.append(mod)
        if modules_to_wire:
            self._container.wire(modules=modules_to_wire)

    async def _register_in_membership(self) -> None:
        """Register this silo in the membership table."""
        now = time.time()
        silo_info = SiloInfo(
            address=self._silo_address,
            status=SiloStatus.ACTIVE,
            last_heartbeat=now,
            start_time=now,
        )
        await self._membership_provider.register_silo(silo_info)

    async def _heartbeat_loop(self) -> None:
        """Periodically update heartbeat in membership table."""
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                try:
                    await self._membership_provider.heartbeat(self._silo_id)
                except Exception:
                    logger.warning(
                        "Heartbeat failed for silo %s",
                        self._silo_id,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            return

    def _install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()

        def _handle_signal() -> None:
            logger.info("Received shutdown signal")
            self._shutdown_task = loop.create_task(self.stop())

        try:
            loop.add_signal_handler(signal.SIGINT, _handle_signal)
            loop.add_signal_handler(signal.SIGTERM, _handle_signal)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler on ProactorEventLoop
            pass
