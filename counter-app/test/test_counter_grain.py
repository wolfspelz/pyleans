"""Tests for the CounterGrain — sample stateful grain."""

import asyncio
from typing import Any

import pytest
from counter_app.counter_grain import CounterGrain, CounterState
from pyleans.grain import _grain_registry, get_grain_class, get_grain_methods
from pyleans.identity import GrainId
from pyleans.providers.membership import MembershipProvider
from pyleans.providers.storage import StorageProvider
from pyleans.server.providers.memory_stream import InMemoryStreamProvider
from pyleans.server.silo import Silo

# --- Fake providers for testing ---


class FakeStorageProvider(StorageProvider):
    """In-memory storage for testing."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict[str, Any], str]] = {}

    async def read(self, grain_type: str, grain_key: str) -> tuple[dict[str, Any], str | None]:
        key = f"{grain_type}/{grain_key}"
        if key in self._store:
            state, etag = self._store[key]
            return state, etag
        return {}, None

    async def write(
        self,
        grain_type: str,
        grain_key: str,
        state: dict[str, Any],
        expected_etag: str | None,
    ) -> str:
        import time

        key = f"{grain_type}/{grain_key}"
        new_etag = str(time.monotonic())
        self._store[key] = (state, new_etag)
        return new_etag

    async def clear(
        self,
        grain_type: str,
        grain_key: str,
        expected_etag: str | None,
    ) -> None:
        key = f"{grain_type}/{grain_key}"
        self._store.pop(key, None)


class FakeMembershipProvider(MembershipProvider):
    """In-memory membership for testing."""

    def __init__(self) -> None:
        self.silos: dict[str, Any] = {}

    async def register_silo(self, silo: Any) -> None:
        self.silos[silo.address.encoded] = silo

    async def unregister_silo(self, silo_id: str) -> None:
        self.silos.pop(silo_id, None)

    async def get_active_silos(self) -> list[Any]:
        return list(self.silos.values())

    async def heartbeat(self, silo_id: str) -> None:
        pass

    async def update_status(self, silo_id: str, status: Any) -> None:
        pass


# --- Fixtures ---


@pytest.fixture(autouse=True)
def _ensure_grain_registered() -> None:  # type: ignore[misc]
    """Make sure CounterGrain is in the registry for each test."""
    _grain_registry["CounterGrain"] = CounterGrain


def make_silo(storage: FakeStorageProvider | None = None) -> Silo:
    s = storage or FakeStorageProvider()
    return Silo(
        grains=[CounterGrain],
        storage_providers={"default": s},
        membership_provider=FakeMembershipProvider(),
        stream_providers={"default": InMemoryStreamProvider()},
        gateway_port=0,
    )


# --- Tests ---


class TestCounterGrainRegistration:
    def test_grain_registered_via_decorator(self) -> None:
        cls = get_grain_class("CounterGrain")
        assert cls is CounterGrain

    def test_grain_type_attribute(self) -> None:
        assert CounterGrain._grain_type == "CounterGrain"  # type: ignore[attr-defined]

    def test_state_type_attribute(self) -> None:
        assert CounterGrain._state_type is CounterState  # type: ignore[attr-defined]

    def test_storage_name_attribute(self) -> None:
        assert CounterGrain._storage_name == "default"  # type: ignore[attr-defined]

    def test_public_methods_discovered(self) -> None:
        methods = get_grain_methods(CounterGrain)
        expected = {"get_value", "increment", "set_value", "reset", "get_silo_info"}
        assert set(methods.keys()) == expected


class TestCounterState:
    def test_default_value_is_zero(self) -> None:
        state = CounterState()
        assert state.value == 0

    def test_custom_value(self) -> None:
        state = CounterState(value=42)
        assert state.value == 42


class TestGetValue:
    async def test_returns_zero_initially(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(CounterGrain, "c1")
        assert await ref.get_value() == 0

        await silo.stop()


class TestIncrement:
    async def test_increment_returns_new_value(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(CounterGrain, "c1")
        assert await ref.increment() == 1
        assert await ref.increment() == 2
        assert await ref.increment() == 3

        await silo.stop()

    async def test_increment_persists(self) -> None:
        storage = FakeStorageProvider()
        silo = make_silo(storage)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(CounterGrain, "c1")
        await ref.increment()
        await ref.increment()

        state_dict, etag = await storage.read("CounterGrain", "c1")
        assert state_dict["value"] == 2
        assert etag is not None

        await silo.stop()


class TestSetValue:
    async def test_set_value(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(CounterGrain, "c1")
        await ref.set_value(42)
        assert await ref.get_value() == 42

        await silo.stop()

    async def test_set_value_persists(self) -> None:
        storage = FakeStorageProvider()
        silo = make_silo(storage)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(CounterGrain, "c1")
        await ref.set_value(99)

        state_dict, _ = await storage.read("CounterGrain", "c1")
        assert state_dict["value"] == 99

        await silo.stop()


class TestReset:
    async def test_reset_to_zero(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(CounterGrain, "c1")
        await ref.increment()
        await ref.increment()
        assert await ref.get_value() == 2

        await ref.reset()
        assert await ref.get_value() == 0

        await silo.stop()


class TestStateSurvivesReactivation:
    async def test_state_survives_deactivation(self) -> None:
        storage = FakeStorageProvider()
        silo = make_silo(storage)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(CounterGrain, "c1")
        await ref.increment()
        await ref.increment()
        await ref.increment()

        gid = GrainId("CounterGrain", "c1")
        await silo.runtime.deactivate_grain(gid)
        assert gid not in silo.runtime.activations

        ref2 = silo.grain_factory.get_grain(CounterGrain, "c1")
        assert await ref2.get_value() == 3

        await silo.stop()

    async def test_state_survives_silo_restart(self) -> None:
        storage = FakeStorageProvider()

        silo1 = make_silo(storage)
        await silo1.start_background()
        ref1 = silo1.grain_factory.get_grain(CounterGrain, "persist")
        await ref1.set_value(77)
        await silo1.stop()

        silo2 = make_silo(storage)
        await silo2.start_background()
        ref2 = silo2.grain_factory.get_grain(CounterGrain, "persist")
        assert await ref2.get_value() == 77
        await silo2.stop()


class TestMultipleCounterInstances:
    async def test_independent_counters(self) -> None:
        silo = make_silo()
        await silo.start_background()

        c1 = silo.grain_factory.get_grain(CounterGrain, "counter-a")
        c2 = silo.grain_factory.get_grain(CounterGrain, "counter-b")
        c3 = silo.grain_factory.get_grain(CounterGrain, "counter-c")

        await c1.increment()
        await c1.increment()
        await c2.set_value(100)
        await c3.increment()

        assert await c1.get_value() == 2
        assert await c2.get_value() == 100
        assert await c3.get_value() == 1

        await silo.stop()

    async def test_concurrent_counters(self) -> None:
        silo = make_silo()
        await silo.start_background()

        refs = [silo.grain_factory.get_grain(CounterGrain, f"c{i}") for i in range(5)]
        await asyncio.gather(*(r.increment() for r in refs))
        results = await asyncio.gather(*(r.get_value() for r in refs))
        assert results == [1, 1, 1, 1, 1]

        await silo.stop()


class TestSiloInfo:
    async def test_get_silo_info_returns_dict(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(CounterGrain, "c1")
        info = await ref.get_silo_info()
        assert isinstance(info, dict)
        assert "silo_id" in info
        assert "grain_count" in info

        await silo.stop()
