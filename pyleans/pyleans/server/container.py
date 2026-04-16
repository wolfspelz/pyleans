"""Dependency injection container for the pyleans framework.

Uses the ``injector`` package for type-hint-based resolution.
Grains declare dependencies as plain ``__init__`` type hints — no
decorators or markers needed.
"""

from injector import Injector, Module, provider, singleton

from pyleans.reference import GrainFactory
from pyleans.server.providers.memory_stream import StreamManager
from pyleans.server.runtime import GrainRuntime
from pyleans.server.silo_management import SiloManagement
from pyleans.server.timer import TimerRegistry


class PyleansModule(Module):
    """Injector module that binds framework services.

    Instances are provided by the Silo at startup via the constructor.
    """

    def __init__(
        self,
        runtime: GrainRuntime,
        grain_factory: GrainFactory,
        timer_registry: TimerRegistry,
        silo_management: SiloManagement,
        stream_manager: StreamManager | None = None,
    ) -> None:
        self._runtime = runtime
        self._grain_factory = grain_factory
        self._timer_registry = timer_registry
        self._silo_management = silo_management
        self._stream_manager = stream_manager

    @provider
    @singleton
    def provide_runtime(self) -> GrainRuntime:
        return self._runtime

    @provider
    @singleton
    def provide_grain_factory(self) -> GrainFactory:
        return self._grain_factory

    @provider
    @singleton
    def provide_timer_registry(self) -> TimerRegistry:
        return self._timer_registry

    @provider
    @singleton
    def provide_silo_management(self) -> SiloManagement:
        return self._silo_management

    @provider
    @singleton
    def provide_stream_manager(self) -> StreamManager:
        if self._stream_manager is None:
            raise RuntimeError("No stream provider configured")
        return self._stream_manager


def create_injector(
    runtime: GrainRuntime,
    grain_factory: GrainFactory,
    timer_registry: TimerRegistry,
    silo_management: SiloManagement,
    stream_manager: StreamManager | None = None,
) -> Injector:
    """Create a configured injector with all framework services bound."""
    module = PyleansModule(
        runtime=runtime,
        grain_factory=grain_factory,
        timer_registry=timer_registry,
        silo_management=silo_management,
        stream_manager=stream_manager,
    )
    return Injector([module])
