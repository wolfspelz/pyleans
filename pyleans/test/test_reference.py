"""Tests for pyleans.reference — grain reference proxy and factory."""

from dataclasses import dataclass

import pytest
from conftest import FakeStorageProvider
from pyleans.grain import _grain_registry, grain
from pyleans.grain_base import Grain
from pyleans.identity import GrainId
from pyleans.providers.storage import StorageProvider
from pyleans.reference import GrainFactory, GrainRef
from pyleans.serialization import JsonSerializer
from pyleans.server.runtime import GrainRuntime


@dataclass
class CounterState:
    value: int = 0


_TEST_GRAINS: list[type] = []


@grain
class CounterGrain(Grain[CounterState]):
    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.write_state()
        return self.state.value


_TEST_GRAINS.append(CounterGrain)


@grain
class GreeterGrain:
    async def greet(self, name: str) -> str:
        return f"Hello, {name}!"


_TEST_GRAINS.append(GreeterGrain)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    _grain_registry.clear()
    for cls in _TEST_GRAINS:
        _grain_registry[cls.__name__] = cls


def make_runtime(storage: StorageProvider | None = None) -> GrainRuntime:
    providers: dict[str, StorageProvider] = {}
    if storage is not None:
        providers["default"] = storage
    return GrainRuntime(
        storage_providers=providers,
        serializer=JsonSerializer(),
    )


class TestGrainRef:
    def test_identity(self) -> None:
        runtime = make_runtime()
        gid = GrainId("CounterGrain", "c1")
        ref = GrainRef(gid, runtime)
        assert ref.identity == gid

    def test_repr(self) -> None:
        runtime = make_runtime()
        gid = GrainId("CounterGrain", "c1")
        ref = GrainRef(gid, runtime)
        assert "CounterGrain" in repr(ref)
        assert "c1" in repr(ref)

    def test_private_attribute_raises(self) -> None:
        runtime = make_runtime()
        gid = GrainId("CounterGrain", "c1")
        ref = GrainRef(gid, runtime)
        with pytest.raises(AttributeError):
            ref._secret  # noqa: B018

    def test_equality_same_grain_id(self) -> None:
        runtime = make_runtime()
        gid = GrainId("CounterGrain", "c1")
        ref1 = GrainRef(gid, runtime)
        ref2 = GrainRef(gid, runtime)
        assert ref1 == ref2

    def test_equality_different_grain_id(self) -> None:
        runtime = make_runtime()
        ref1 = GrainRef(GrainId("CounterGrain", "c1"), runtime)
        ref2 = GrainRef(GrainId("CounterGrain", "c2"), runtime)
        assert ref1 != ref2

    def test_hashable(self) -> None:
        runtime = make_runtime()
        gid = GrainId("CounterGrain", "c1")
        ref = GrainRef(gid, runtime)
        d = {ref: "value"}
        assert d[GrainRef(gid, runtime)] == "value"

    async def test_proxy_call_dispatches(self) -> None:
        runtime = make_runtime()
        gid = GrainId("GreeterGrain", "g1")
        ref = GrainRef(gid, runtime)
        result = await ref.greet("World")
        assert result == "Hello, World!"
        await runtime.stop()

    async def test_proxy_call_with_state(self) -> None:
        runtime = make_runtime(FakeStorageProvider())
        gid = GrainId("CounterGrain", "c1")
        ref = GrainRef(gid, runtime)

        assert await ref.get_value() == 0
        assert await ref.increment() == 1
        assert await ref.increment() == 2
        assert await ref.get_value() == 2
        await runtime.stop()

    async def test_multiple_refs_share_activation(self) -> None:
        runtime = make_runtime(FakeStorageProvider())
        gid = GrainId("CounterGrain", "c1")
        ref1 = GrainRef(gid, runtime)
        ref2 = GrainRef(gid, runtime)

        await ref1.increment()
        result = await ref2.get_value()
        assert result == 1
        assert len(runtime.activations) == 1
        await runtime.stop()


class TestGrainFactory:
    def test_get_grain_returns_ref(self) -> None:
        runtime = make_runtime()
        factory = GrainFactory(runtime)
        ref = factory.get_grain(CounterGrain, "c1")
        assert isinstance(ref, GrainRef)
        assert ref.identity == GrainId("CounterGrain", "c1")

    def test_get_grain_does_not_activate(self) -> None:
        runtime = make_runtime()
        factory = GrainFactory(runtime)
        factory.get_grain(CounterGrain, "c1")
        assert len(runtime.activations) == 0

    async def test_factory_ref_dispatches(self) -> None:
        runtime = make_runtime()
        factory = GrainFactory(runtime)
        ref = factory.get_grain(GreeterGrain, "g1")
        result = await ref.greet("Factory")
        assert result == "Hello, Factory!"
        await runtime.stop()

    def test_factory_different_keys(self) -> None:
        runtime = make_runtime()
        factory = GrainFactory(runtime)
        ref1 = factory.get_grain(CounterGrain, "c1")
        ref2 = factory.get_grain(CounterGrain, "c2")
        assert ref1.identity != ref2.identity
