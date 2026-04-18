"""Tests for DI container (injector-based)."""

from injector import Injector
from pyleans.reference import GrainFactory
from pyleans.serialization import JsonSerializer
from pyleans.server.container import create_injector
from pyleans.server.providers.memory_stream import InMemoryStreamProvider, StreamManager
from pyleans.server.runtime import GrainRuntime
from pyleans.server.silo_management import SiloManagement
from pyleans.server.timer import TimerRegistry


def _make_injector(
    stream_manager: StreamManager | None = None,
) -> tuple[Injector, GrainRuntime, GrainFactory, TimerRegistry, SiloManagement]:
    """Create a minimal injector with fake services for testing."""
    runtime = GrainRuntime(
        storage_providers={},
        serializer=JsonSerializer(),
    )
    factory = GrainFactory(runtime=runtime)
    timer_registry = TimerRegistry(runtime=runtime)
    silo_management = SiloManagement.__new__(SiloManagement)
    injector = create_injector(
        runtime=runtime,
        grain_factory=factory,
        timer_registry=timer_registry,
        silo_management=silo_management,
        stream_manager=stream_manager,
    )
    return injector, runtime, factory, timer_registry, silo_management


class TestInjectorCreation:
    def test_create_injector(self) -> None:
        injector, *_ = _make_injector()
        assert injector is not None

    def test_resolve_runtime(self) -> None:
        injector, runtime, *_ = _make_injector()
        assert injector.get(GrainRuntime) is runtime

    def test_resolve_grain_factory(self) -> None:
        injector, _, factory, *_ = _make_injector()
        assert injector.get(GrainFactory) is factory

    def test_resolve_timer_registry(self) -> None:
        injector, _, _, timer_registry, _ = _make_injector()
        assert injector.get(TimerRegistry) is timer_registry

    def test_resolve_silo_management(self) -> None:
        injector, _, _, _, silo_management = _make_injector()
        assert injector.get(SiloManagement) is silo_management

    def test_singleton_returns_same_instance(self) -> None:
        injector, runtime, *_ = _make_injector()
        r1 = injector.get(GrainRuntime)
        r2 = injector.get(GrainRuntime)
        assert r1 is r2 is runtime

    def test_resolve_stream_manager(self) -> None:
        sm = StreamManager(InMemoryStreamProvider())
        injector, *_ = _make_injector(stream_manager=sm)
        assert injector.get(StreamManager) is sm


class TestInjectorGrainCreation:
    def test_resolve_grain_with_typed_dependency(self) -> None:
        """Grains decorated with @grain get @inject applied automatically."""
        from pyleans.grain import _grain_registry, grain
        from pyleans.grain_base import Grain

        _grain_registry.clear()

        @grain
        class DITestGrain(Grain[None]):
            def __init__(self, mgmt: SiloManagement) -> None:
                self.mgmt = mgmt

            async def get_mgmt(self) -> SiloManagement:
                return self.mgmt

        injector, _, _, _, silo_management = _make_injector()
        instance = injector.get(DITestGrain)
        assert instance.mgmt is silo_management
