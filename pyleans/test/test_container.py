"""Tests for DI container."""

import logging

from pyleans.reference import GrainFactory
from pyleans.server.container import PyleansContainer
from pyleans.server.providers.memory_stream import InMemoryStreamProvider, StreamManager
from pyleans.server.runtime import GrainRuntime
from pyleans.server.timer import TimerRegistry


class TestPyleansContainer:
    def test_container_creates(self) -> None:
        container = PyleansContainer()
        assert container is not None

    def test_runtime_provider(self) -> None:
        container = PyleansContainer()
        runtime = container.runtime()
        assert isinstance(runtime, GrainRuntime)

    def test_grain_factory_provider(self) -> None:
        container = PyleansContainer()
        factory = container.grain_factory()
        assert isinstance(factory, GrainFactory)

    def test_timer_registry_provider(self) -> None:
        container = PyleansContainer()
        registry = container.timer_registry()
        assert isinstance(registry, TimerRegistry)

    def test_logger_provider(self) -> None:
        container = PyleansContainer()
        container.config.logger_name.from_value("pyleans.test")
        log = container.logger()
        assert isinstance(log, logging.Logger)
        assert log.name == "pyleans.test"

    def test_singleton_returns_same_instance(self) -> None:
        container = PyleansContainer()
        r1 = container.runtime()
        r2 = container.runtime()
        assert r1 is r2

    def test_factory_uses_runtime_singleton(self) -> None:
        container = PyleansContainer()
        runtime = container.runtime()
        factory = container.grain_factory()
        assert factory._runtime is runtime

    def test_timer_registry_uses_runtime(self) -> None:
        container = PyleansContainer()
        runtime = container.runtime()
        registry = container.timer_registry()
        assert registry._runtime is runtime

    def test_stream_manager_provider(self) -> None:
        from dependency_injector import providers as di_providers

        container = PyleansContainer()
        container.stream_provider.override(di_providers.Object(InMemoryStreamProvider()))
        manager = container.stream_manager()
        assert isinstance(manager, StreamManager)


class TestContainerExtension:
    def test_user_can_extend_container(self) -> None:
        from dependency_injector import providers

        class AppContainer(PyleansContainer):
            greeting = providers.Factory(str, "hello")

        container = AppContainer()
        assert container.greeting() == "hello"
        assert isinstance(container.runtime(), GrainRuntime)

    def test_user_can_override_providers(self) -> None:
        from dependency_injector import providers

        container = PyleansContainer()
        container.runtime.override(providers.Factory(dict))
        result = container.runtime()
        assert isinstance(result, dict)
