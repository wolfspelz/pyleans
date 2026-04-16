"""Tests for StringCacheGrain and system_grains()."""

import asyncio
from typing import Any

from conftest import FakeStorageProvider
from pyleans.identity import GrainId, SiloStatus
from pyleans.providers.membership import MembershipProvider
from pyleans.server.grains import StringCacheGrain, system_grains
from pyleans.server.providers.memory_stream import InMemoryStreamProvider
from pyleans.server.silo import Silo

# --- Fake membership ---


class FakeMembershipProvider(MembershipProvider):
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

    async def update_status(self, silo_id: str, status: SiloStatus) -> None:
        pass


# --- Helpers ---


def make_silo(storage: FakeStorageProvider | None = None) -> Silo:
    s = storage or FakeStorageProvider()
    return Silo(
        grains=[*system_grains()],
        storage_providers={"default": s},
        membership_provider=FakeMembershipProvider(),
        stream_providers={"default": InMemoryStreamProvider()},
        gateway_port=0,
    )


# --- Tests ---


class TestSystemGrains:
    def test_returns_list_with_string_cache(self) -> None:
        result = system_grains()
        assert StringCacheGrain in result

    def test_can_spread_into_grain_list(self) -> None:
        grains = [*system_grains()]
        assert StringCacheGrain in grains


class TestStringCacheSet:
    async def test_set_and_get(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(StringCacheGrain, "k1")
        await ref.set("hello")
        assert await ref.get() == "hello"

        await silo.stop()

    async def test_set_overwrites(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(StringCacheGrain, "k1")
        await ref.set("first")
        await ref.set("second")
        assert await ref.get() == "second"

        await silo.stop()


class TestStringCacheGet:
    async def test_get_default_empty_string(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(StringCacheGrain, "new-key")
        assert await ref.get() == ""

        await silo.stop()


class TestStringCacheDelete:
    async def test_delete_clears_state(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(StringCacheGrain, "k1")
        await ref.set("data")
        await ref.delete()
        assert await ref.get() == ""

        await silo.stop()


class TestStringCacheDeactivate:
    async def test_deactivate_removes_from_memory(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(StringCacheGrain, "k1")
        await ref.set("persisted")

        gid = GrainId("StringCacheGrain", "k1")
        assert gid in silo.runtime.activations

        await ref.deactivate()
        # Give the event loop a tick for deactivation to complete
        await asyncio.sleep(0.05)
        assert gid not in silo.runtime.activations

        await silo.stop()

    async def test_reactivate_from_persistence(self) -> None:
        storage = FakeStorageProvider()
        silo = make_silo(storage)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(StringCacheGrain, "k1")
        await ref.set("persisted-value")
        await ref.deactivate()
        await asyncio.sleep(0.05)

        # Re-access — should reactivate from storage
        ref2 = silo.grain_factory.get_grain(StringCacheGrain, "k1")
        assert await ref2.get() == "persisted-value"

        await silo.stop()


class TestStringCachePersistence:
    async def test_state_survives_silo_restart(self) -> None:
        storage = FakeStorageProvider()

        silo1 = make_silo(storage)
        await silo1.start_background()
        ref = silo1.grain_factory.get_grain(StringCacheGrain, "survive")
        await ref.set("across-restart")
        await silo1.stop()

        silo2 = make_silo(storage)
        await silo2.start_background()
        ref2 = silo2.grain_factory.get_grain(StringCacheGrain, "survive")
        assert await ref2.get() == "across-restart"
        await silo2.stop()

    async def test_multiple_keys_independent(self) -> None:
        silo = make_silo()
        await silo.start_background()

        a = silo.grain_factory.get_grain(StringCacheGrain, "a")
        b = silo.grain_factory.get_grain(StringCacheGrain, "b")
        await a.set("alpha")
        await b.set("beta")
        assert await a.get() == "alpha"
        assert await b.get() == "beta"

        await silo.stop()
