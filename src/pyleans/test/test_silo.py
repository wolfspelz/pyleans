"""Tests for pyleans.server.silo — Silo main entry point."""

import asyncio
from dataclasses import dataclass

import pytest
from pyleans.grain import _grain_registry, grain
from pyleans.grain_base import Grain
from pyleans.identity import GrainId, SiloStatus
from pyleans.net import InMemoryNetwork
from pyleans.providers.membership import MembershipProvider
from pyleans.providers.storage import StorageProvider
from pyleans.providers.streaming import StreamProvider
from pyleans.reference import GrainFactory
from pyleans.server.providers.memory_stream import InMemoryStreamProvider
from pyleans.server.runtime import GrainRuntime
from pyleans.server.silo import Silo
from pyleans.server.timer import TimerRegistry
from pyleans.testing import InMemoryMembershipProvider

from conftest import FakeStorageProvider

# --- Test grains ---


@dataclass
class CounterState:
    value: int = 0


@grain
class SiloCounterGrain(Grain[CounterState]):
    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.write_state()
        return self.state.value


@grain
class SiloStatelessGrain:
    async def greet(self, name: str) -> str:
        return f"Hello, {name}!"


@grain
class SiloLifecycleGrain:
    def __init__(self) -> None:
        self.activated = False
        self.deactivated = False

    async def on_activate(self) -> None:
        self.activated = True

    async def on_deactivate(self) -> None:
        self.deactivated = True

    async def is_activated(self) -> bool:
        return self.activated


_TEST_GRAINS = [SiloCounterGrain, SiloStatelessGrain, SiloLifecycleGrain]

_ORIGINAL_CLASSES: dict[str, type] = {}
for _cls in _TEST_GRAINS:
    _ORIGINAL_CLASSES[_cls.__name__] = _cls


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    _grain_registry.clear()
    for name, cls in _ORIGINAL_CLASSES.items():
        _grain_registry[name] = cls


# --- Fake membership provider ---


FakeMembershipProvider = InMemoryMembershipProvider


# --- Helpers ---


def make_silo(
    network: InMemoryNetwork,
    grains: list[type] | None = None,
    storage: StorageProvider | None = None,
    membership: MembershipProvider | None = None,
    stream: StreamProvider | None = None,
    idle_timeout: float = 900.0,
) -> Silo:
    storage_providers = {"default": storage} if storage else {"default": FakeStorageProvider()}
    membership_provider = membership or FakeMembershipProvider()
    stream_providers = {"default": stream} if stream else {"default": InMemoryStreamProvider()}
    return Silo(
        grains=grains or _TEST_GRAINS,
        storage_providers=storage_providers,
        membership_provider=membership_provider,
        stream_providers=stream_providers,
        idle_timeout=idle_timeout,
        gateway_port=0,
        network=network,
    )


# --- Tests ---


class TestSiloStartStop:
    async def test_start_and_stop(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()
        assert silo.started is True
        await silo.stop()
        assert silo.started is False

    async def test_stop_without_start_is_noop(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.stop()
        assert silo.started is False

    async def test_double_stop_is_safe(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()
        await silo.stop()
        await silo.stop()
        assert silo.started is False

    async def test_start_blocking_stops_on_stop_event(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)

        async def stop_after_delay() -> None:
            await asyncio.sleep(0.05)
            await silo.stop()

        stop_task = asyncio.create_task(stop_after_delay())
        await silo.start()
        await stop_task
        assert silo.started is False


class TestSiloProperties:
    async def test_grain_factory_property(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        assert isinstance(silo.grain_factory, GrainFactory)

    async def test_runtime_property(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        assert isinstance(silo.runtime, GrainRuntime)

    async def test_timer_registry_property(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        assert isinstance(silo.timer_registry, TimerRegistry)


class TestGrainRegistration:
    async def test_grain_classes_registered(self, network: InMemoryNetwork) -> None:
        _grain_registry.clear()
        silo = make_silo(network)
        await silo.start_background()
        assert "SiloCounterGrain" in _grain_registry
        assert "SiloStatelessGrain" in _grain_registry
        assert "SiloLifecycleGrain" in _grain_registry
        await silo.stop()

    async def test_empty_grain_list(self, network: InMemoryNetwork) -> None:
        _grain_registry.clear()
        silo = make_silo(network, grains=[])
        await silo.start_background()
        assert silo.started is True
        await silo.stop()


class TestGrainCalls:
    async def test_stateless_grain_call(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(SiloStatelessGrain, "s1")
        result = await ref.greet("World")
        assert result == "Hello, World!"

        await silo.stop()

    async def test_stateful_grain_call(self, network: InMemoryNetwork) -> None:
        storage = FakeStorageProvider()
        silo = make_silo(network, storage=storage)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(SiloCounterGrain, "c1")
        assert await ref.get_value() == 0
        assert await ref.increment() == 1
        assert await ref.increment() == 2
        assert await ref.get_value() == 2

        await silo.stop()

    async def test_lifecycle_hooks_called(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(SiloLifecycleGrain, "l1")
        assert await ref.is_activated() is True

        gid = GrainId("SiloLifecycleGrain", "l1")
        instance = silo.runtime.activations[gid].instance
        await silo.stop()
        assert instance.deactivated is True

    async def test_multiple_grains_independent(self, network: InMemoryNetwork) -> None:
        storage = FakeStorageProvider()
        silo = make_silo(network, storage=storage)
        await silo.start_background()

        c1 = silo.grain_factory.get_grain(SiloCounterGrain, "c1")
        c2 = silo.grain_factory.get_grain(SiloCounterGrain, "c2")

        await c1.increment()
        await c1.increment()
        await c2.increment()

        assert await c1.get_value() == 2
        assert await c2.get_value() == 1

        await silo.stop()


class TestMembershipIntegration:
    async def test_silo_registers_on_start(self, network: InMemoryNetwork) -> None:
        # Arrange
        membership = FakeMembershipProvider()
        silo = make_silo(network, membership=membership)

        # Act
        await silo.start_background()

        # Assert
        snapshot = await membership.read_all()
        assert len(snapshot.silos) == 1
        assert snapshot.silos[0].status == SiloStatus.ACTIVE

        await silo.stop()

    async def test_silo_unregisters_on_stop(self, network: InMemoryNetwork) -> None:
        # Arrange
        membership = FakeMembershipProvider()
        silo = make_silo(network, membership=membership)
        await silo.start_background()

        # Act
        await silo.stop()

        # Assert
        assert (await membership.read_all()).silos == []

    async def test_silo_status_shutting_down_before_unregister(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange
        membership = FakeMembershipProvider()
        silo = make_silo(network, membership=membership)
        await silo.start_background()
        statuses: list[SiloStatus] = []
        original_try_update = membership.try_update_silo

        async def track_status(silo_info):  # type: ignore[no-untyped-def]
            statuses.append(silo_info.status)
            return await original_try_update(silo_info)

        membership.try_update_silo = track_status  # type: ignore[assignment]

        # Act
        await silo.stop()

        # Assert
        assert SiloStatus.SHUTTING_DOWN in statuses


class TestHeartbeat:
    async def test_heartbeat_runs_periodically(self, network: InMemoryNetwork) -> None:
        # Arrange - membership agent drives ``i_am_alive`` writes; use a short
        # interval so the test doesn't sit on the 30s production default.
        from pyleans.cluster.failure_detector import FailureDetectorOptions

        membership = FakeMembershipProvider()
        silo = Silo(
            grains=_TEST_GRAINS,
            storage_providers={"default": FakeStorageProvider()},
            membership_provider=membership,
            stream_providers={"default": InMemoryStreamProvider()},
            gateway_port=0,
            network=network,
            failure_detector_options=FailureDetectorOptions(
                i_am_alive_interval=0.02,
                table_poll_interval=0.02,
            ),
        )

        try:
            await silo.start_background()
            await asyncio.sleep(0.07)
            assert membership.heartbeat_count >= 2
        finally:
            await silo.stop()

    async def test_heartbeat_cancelled_on_stop(self, network: InMemoryNetwork) -> None:
        from pyleans.cluster.failure_detector import FailureDetectorOptions

        membership = FakeMembershipProvider()
        silo = Silo(
            grains=_TEST_GRAINS,
            storage_providers={"default": FakeStorageProvider()},
            membership_provider=membership,
            stream_providers={"default": InMemoryStreamProvider()},
            gateway_port=0,
            network=network,
            failure_detector_options=FailureDetectorOptions(
                i_am_alive_interval=0.02,
                table_poll_interval=0.02,
            ),
        )
        await silo.start_background()
        await silo.stop()

        count_after_stop = membership.heartbeat_count
        await asyncio.sleep(0.05)
        assert membership.heartbeat_count == count_after_stop


class TestGracefulShutdown:
    async def test_stop_deactivates_all_grains(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        ref1 = silo.grain_factory.get_grain(SiloStatelessGrain, "s1")
        ref2 = silo.grain_factory.get_grain(SiloStatelessGrain, "s2")
        await ref1.greet("a")
        await ref2.greet("b")
        assert len(silo.runtime.activations) == 2

        await silo.stop()
        assert len(silo.runtime.activations) == 0

    async def test_state_persisted_on_stop(self, network: InMemoryNetwork) -> None:
        storage = FakeStorageProvider()
        silo = make_silo(network, storage=storage)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(SiloCounterGrain, "c1")
        await ref.increment()
        await ref.increment()
        await ref.increment()

        await silo.stop()

        state_dict, etag = await storage.read("SiloCounterGrain", "c1")
        assert state_dict["value"] == 3
        assert etag is not None


class TestIntegrationEndToEnd:
    async def test_start_call_stop_restart_state_persisted(self, network: InMemoryNetwork) -> None:
        """Full integration: start, call grain, stop, restart, verify state."""
        storage = FakeStorageProvider()
        membership = FakeMembershipProvider()

        silo1 = make_silo(network, storage=storage, membership=membership)
        await silo1.start_background()

        ref = silo1.grain_factory.get_grain(SiloCounterGrain, "persist-test")
        await ref.increment()
        await ref.increment()
        assert await ref.get_value() == 2

        await silo1.stop()

        # Start a new silo with the same storage
        silo2 = make_silo(network, storage=storage, membership=membership)
        await silo2.start_background()

        ref2 = silo2.grain_factory.get_grain(SiloCounterGrain, "persist-test")
        assert await ref2.get_value() == 2
        assert await ref2.increment() == 3

        await silo2.stop()

    async def test_concurrent_grain_calls(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(SiloStatelessGrain, "conc")
        results = await asyncio.gather(
            ref.greet("A"),
            ref.greet("B"),
            ref.greet("C"),
        )
        assert set(results) == {"Hello, A!", "Hello, B!", "Hello, C!"}

        await silo.stop()


class TestDefaultProviders:
    def test_defaults_applied_when_no_providers_given(self) -> None:
        """Verify that defaults are created when no providers are passed."""
        from pyleans.server.providers.file_storage import FileStorageProvider
        from pyleans.server.providers.memory_stream import InMemoryStreamProvider
        from pyleans.server.providers.yaml_membership import YamlMembershipProvider

        silo = Silo(grains=[])
        assert isinstance(silo._storage_providers["default"], FileStorageProvider)
        assert isinstance(silo._membership_provider, YamlMembershipProvider)
        assert isinstance(silo._stream_providers["default"], InMemoryStreamProvider)

    def test_custom_providers_used(self) -> None:
        storage = FakeStorageProvider()
        membership = FakeMembershipProvider()
        stream = InMemoryStreamProvider()

        silo = Silo(
            grains=[],
            storage_providers={"custom": storage},
            membership_provider=membership,
            stream_providers={"custom": stream},
        )
        assert silo._storage_providers == {"custom": storage}
        assert silo._membership_provider is membership
        assert silo._stream_providers == {"custom": stream}


class TestSiloImport:
    def test_import_from_server_package(self) -> None:
        from pyleans.server import Silo as SiloFromPackage

        assert SiloFromPackage is Silo

    def test_import_public_api(self) -> None:
        import pyleans

        assert hasattr(pyleans, "GrainId")
        assert hasattr(pyleans, "GrainRef")
        assert hasattr(pyleans, "GrainFactory")
        assert hasattr(pyleans, "grain")
