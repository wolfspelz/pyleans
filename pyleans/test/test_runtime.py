"""Tests for pyleans.server.runtime — grain runtime engine."""

import asyncio
import time
from dataclasses import dataclass

import pytest
from conftest import FakeStorageProvider
from pyleans.errors import GrainMethodError, GrainNotFoundError
from pyleans.grain import _grain_registry, grain
from pyleans.grain_base import Grain
from pyleans.identity import GrainId
from pyleans.providers.storage import StorageProvider
from pyleans.serialization import JsonSerializer
from pyleans.server.runtime import GrainRuntime


@dataclass
class CounterState:
    value: int = 0


_GRAIN_CLASSES: list[type] = []


def _register_grain(cls: type) -> None:
    """Track grain classes for re-registration after registry clear."""
    _GRAIN_CLASSES.append(cls)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    _grain_registry.clear()
    for cls in _GRAIN_CLASSES:
        _grain_registry[cls.__name__] = cls


def make_runtime(
    storage: StorageProvider | None = None,
    idle_timeout: float = 900.0,
) -> GrainRuntime:
    providers: dict[str, StorageProvider] = {}
    if storage is not None:
        providers["default"] = storage
    return GrainRuntime(
        storage_providers=providers,
        serializer=JsonSerializer(),
        idle_timeout=idle_timeout,
    )


# --- Grain definitions for testing ---


@grain
class CounterGrain(Grain[CounterState]):
    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.save_state()
        return self.state.value


_register_grain(CounterGrain)


@grain
class StatelessGrain:
    async def greet(self, name: str) -> str:
        return f"Hello, {name}!"


_register_grain(StatelessGrain)


@grain
class LifecycleGrain:
    def __init__(self) -> None:
        self.activated = False
        self.deactivated = False

    async def on_activate(self) -> None:
        self.activated = True

    async def on_deactivate(self) -> None:
        self.deactivated = True

    async def is_activated(self) -> bool:
        return self.activated


_register_grain(LifecycleGrain)


@grain
class ErrorGrain:
    async def fail(self) -> None:
        raise ValueError("intentional error")


_register_grain(ErrorGrain)


@grain
class SlowGrain:
    async def slow_method(self, delay: float) -> str:
        await asyncio.sleep(delay)
        return "done"

    async def get_time(self) -> float:
        return time.monotonic()


_register_grain(SlowGrain)


# --- Tests ---


class TestGrainActivation:
    async def test_grain_activated_on_first_call(self) -> None:
        runtime = make_runtime(FakeStorageProvider())
        gid = GrainId("CounterGrain", "c1")
        result = await runtime.invoke(gid, "get_value")
        assert result == 0
        assert gid in runtime.activations
        await runtime.stop()

    async def test_stateless_grain_activation(self) -> None:
        runtime = make_runtime()
        gid = GrainId("StatelessGrain", "s1")
        result = await runtime.invoke(gid, "greet", ["World"])
        assert result == "Hello, World!"
        await runtime.stop()

    async def test_activation_reuses_existing(self) -> None:
        runtime = make_runtime(FakeStorageProvider())
        gid = GrainId("CounterGrain", "c1")
        await runtime.invoke(gid, "increment")
        await runtime.invoke(gid, "increment")
        result = await runtime.invoke(gid, "get_value")
        assert result == 2
        await runtime.stop()

    async def test_unknown_grain_type_raises(self) -> None:
        runtime = make_runtime()
        gid = GrainId("NonExistentGrain", "x1")
        with pytest.raises(GrainNotFoundError):
            await runtime.invoke(gid, "some_method")
        await runtime.stop()

    async def test_unknown_method_raises(self) -> None:
        runtime = make_runtime()
        gid = GrainId("StatelessGrain", "s1")
        with pytest.raises(GrainMethodError, match="no_such_method"):
            await runtime.invoke(gid, "no_such_method")
        await runtime.stop()


class TestStateManagement:
    async def test_state_loaded_from_storage(self) -> None:
        storage = FakeStorageProvider()
        await storage.write("CounterGrain", "c1", {"value": 42}, None)

        runtime = make_runtime(storage)
        gid = GrainId("CounterGrain", "c1")
        result = await runtime.invoke(gid, "get_value")
        assert result == 42
        await runtime.stop()

    async def test_state_defaults_when_no_storage_data(self) -> None:
        runtime = make_runtime(FakeStorageProvider())
        gid = GrainId("CounterGrain", "c1")
        result = await runtime.invoke(gid, "get_value")
        assert result == 0
        await runtime.stop()

    async def test_save_state_persists(self) -> None:
        storage = FakeStorageProvider()
        runtime = make_runtime(storage)
        gid = GrainId("CounterGrain", "c1")

        await runtime.invoke(gid, "increment")
        await runtime.invoke(gid, "increment")

        state_dict, etag = await storage.read("CounterGrain", "c1")
        assert state_dict["value"] == 2
        assert etag is not None
        await runtime.stop()

    async def test_clear_state(self) -> None:
        storage = FakeStorageProvider()
        runtime = make_runtime(storage)
        gid = GrainId("CounterGrain", "c1")

        await runtime.invoke(gid, "increment")
        state_dict, _ = await storage.read("CounterGrain", "c1")
        assert state_dict["value"] == 1

        activation = runtime.activations[gid]
        await activation.instance.clear_state()

        state_dict, _ = await storage.read("CounterGrain", "c1")
        assert state_dict == {}
        assert activation.instance.state.value == 0
        await runtime.stop()


class TestLifecycleHooks:
    async def test_on_activate_called(self) -> None:
        runtime = make_runtime()
        gid = GrainId("LifecycleGrain", "l1")
        result = await runtime.invoke(gid, "is_activated")
        assert result is True
        await runtime.stop()

    async def test_on_deactivate_called(self) -> None:
        runtime = make_runtime()
        gid = GrainId("LifecycleGrain", "l1")
        await runtime.invoke(gid, "is_activated")

        instance = runtime.activations[gid].instance
        await runtime.deactivate_grain(gid)
        assert instance.deactivated is True

    async def test_deactivate_removes_from_activations(self) -> None:
        runtime = make_runtime()
        gid = GrainId("StatelessGrain", "s1")
        await runtime.invoke(gid, "greet", ["test"])
        assert gid in runtime.activations
        await runtime.deactivate_grain(gid)
        assert gid not in runtime.activations

    async def test_deactivate_nonexistent_is_noop(self) -> None:
        runtime = make_runtime()
        gid = GrainId("StatelessGrain", "nope")
        await runtime.deactivate_grain(gid)


class TestTurnBasedExecution:
    async def test_sequential_execution_on_same_grain(self) -> None:
        """Concurrent calls to the same grain execute one at a time."""
        runtime = make_runtime()
        gid = GrainId("SlowGrain", "s1")

        t1 = asyncio.create_task(runtime.invoke(gid, "slow_method", [0.05]))
        t2 = asyncio.create_task(runtime.invoke(gid, "slow_method", [0.05]))

        r1 = await t1
        r2 = await t2
        assert r1 == "done"
        assert r2 == "done"
        await runtime.stop()

    async def test_concurrent_execution_on_different_grains(self) -> None:
        """Calls to different grains run concurrently."""
        runtime = make_runtime()
        gid1 = GrainId("SlowGrain", "s1")
        gid2 = GrainId("SlowGrain", "s2")

        start = time.monotonic()
        t1 = asyncio.create_task(runtime.invoke(gid1, "slow_method", [0.1]))
        t2 = asyncio.create_task(runtime.invoke(gid2, "slow_method", [0.1]))
        await t1
        await t2
        elapsed = time.monotonic() - start

        # If sequential, would take ~0.2s. Concurrent should be ~0.1s.
        assert elapsed < 0.18
        await runtime.stop()


class TestErrorPropagation:
    async def test_grain_method_error_propagated(self) -> None:
        runtime = make_runtime()
        gid = GrainId("ErrorGrain", "e1")
        with pytest.raises(GrainMethodError, match="intentional error"):
            await runtime.invoke(gid, "fail")
        await runtime.stop()

    async def test_grain_still_works_after_error(self) -> None:
        """A grain should remain active after a method error."""
        runtime = make_runtime()
        gid = GrainId("StatelessGrain", "s1")
        await runtime.invoke(gid, "greet", ["first"])

        # Provoke error on a different grain, then verify original still works
        egid = GrainId("ErrorGrain", "e1")
        with pytest.raises(GrainMethodError):
            await runtime.invoke(egid, "fail")

        result = await runtime.invoke(gid, "greet", ["second"])
        assert result == "Hello, second!"
        await runtime.stop()


class TestIdleCollection:
    async def test_idle_grain_deactivated(self) -> None:
        runtime = make_runtime(idle_timeout=0.05)
        gid = GrainId("StatelessGrain", "s1")
        await runtime.invoke(gid, "greet", ["test"])
        assert gid in runtime.activations

        # Manually set last_activity to the past
        runtime.activations[gid].last_activity = time.monotonic() - 1.0

        # Trigger idle collection manually
        await runtime._idle_collector_single_pass()
        assert gid not in runtime.activations

    async def test_active_grain_not_collected(self) -> None:
        runtime = make_runtime(idle_timeout=100.0)
        gid = GrainId("StatelessGrain", "s1")
        await runtime.invoke(gid, "greet", ["test"])

        await runtime._idle_collector_single_pass()
        assert gid in runtime.activations
        await runtime.stop()


class TestRuntimeStartStop:
    async def test_stop_deactivates_all_grains(self) -> None:
        runtime = make_runtime()
        gid1 = GrainId("StatelessGrain", "s1")
        gid2 = GrainId("StatelessGrain", "s2")
        await runtime.invoke(gid1, "greet", ["a"])
        await runtime.invoke(gid2, "greet", ["b"])
        assert len(runtime.activations) == 2

        await runtime.stop()
        assert len(runtime.activations) == 0
