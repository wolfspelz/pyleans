"""Tests for grain timers."""

import asyncio

import pytest
from pyleans.grain import _grain_registry, grain
from pyleans.identity import GrainId
from pyleans.serialization import JsonSerializer
from pyleans.server.runtime import GrainRuntime
from pyleans.server.timer import TimerRegistry

_TEST_GRAINS: list[type] = []


@grain
class TimerTestGrain:
    def __init__(self) -> None:
        self.tick_count = 0

    async def on_tick(self) -> None:
        self.tick_count += 1

    async def get_ticks(self) -> int:
        return self.tick_count


_TEST_GRAINS.append(TimerTestGrain)


@grain
class ErrorTimerGrain:
    async def bad_tick(self) -> None:
        raise ValueError("timer error")

    async def ping(self) -> str:
        return "pong"


_TEST_GRAINS.append(ErrorTimerGrain)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:  # type: ignore[misc]
    _grain_registry.clear()
    for cls in _TEST_GRAINS:
        _grain_registry[cls.__name__] = cls


def make_runtime() -> GrainRuntime:
    return GrainRuntime(
        storage_providers={},
        serializer=JsonSerializer(),
    )


class TestTimerRegistration:
    async def test_register_returns_timer_id(self) -> None:
        runtime = make_runtime()
        registry = TimerRegistry(runtime)
        gid = GrainId("TimerTestGrain", "t1")

        # Activate grain first
        await runtime.invoke(gid, "get_ticks")

        timer_id = registry.register_timer(gid, "on_tick", interval=10.0)
        assert isinstance(timer_id, str)
        assert len(timer_id) > 0

        registry.unregister_all(gid)
        await runtime.stop()

    async def test_unregister_timer(self) -> None:
        runtime = make_runtime()
        registry = TimerRegistry(runtime)
        gid = GrainId("TimerTestGrain", "t1")
        await runtime.invoke(gid, "get_ticks")

        timer_id = registry.register_timer(gid, "on_tick", interval=10.0)
        registry.unregister_timer(timer_id)

        # Timer should be removed
        assert timer_id not in registry._timers
        await runtime.stop()

    async def test_unregister_nonexistent_is_noop(self) -> None:
        runtime = make_runtime()
        registry = TimerRegistry(runtime)
        registry.unregister_timer("nonexistent-id")
        await runtime.stop()

    async def test_unregister_all(self) -> None:
        runtime = make_runtime()
        registry = TimerRegistry(runtime)
        gid = GrainId("TimerTestGrain", "t1")
        await runtime.invoke(gid, "get_ticks")

        registry.register_timer(gid, "on_tick", interval=10.0)
        registry.register_timer(gid, "on_tick", interval=20.0)
        registry.unregister_all(gid)

        assert gid not in registry._grain_timers
        assert len(registry._timers) == 0
        await runtime.stop()


class TestTimerExecution:
    async def test_timer_fires_at_interval(self) -> None:
        runtime = make_runtime()
        registry = TimerRegistry(runtime)
        gid = GrainId("TimerTestGrain", "t1")
        await runtime.invoke(gid, "get_ticks")

        registry.register_timer(gid, "on_tick", interval=0.03, due_time=0.0)

        # Wait enough time for a few ticks
        await asyncio.sleep(0.12)
        ticks = await runtime.invoke(gid, "get_ticks")
        assert ticks >= 2

        registry.unregister_all(gid)
        await runtime.stop()

    async def test_timer_respects_due_time(self) -> None:
        runtime = make_runtime()
        registry = TimerRegistry(runtime)
        gid = GrainId("TimerTestGrain", "t1")
        await runtime.invoke(gid, "get_ticks")

        registry.register_timer(gid, "on_tick", interval=0.02, due_time=0.08)

        # Before due time
        await asyncio.sleep(0.04)
        ticks_before = await runtime.invoke(gid, "get_ticks")
        assert ticks_before == 0

        # After due time + some intervals
        await asyncio.sleep(0.12)
        ticks_after = await runtime.invoke(gid, "get_ticks")
        assert ticks_after >= 1

        registry.unregister_all(gid)
        await runtime.stop()


class TestTimerErrorHandling:
    async def test_error_in_callback_does_not_crash_timer(self) -> None:
        runtime = make_runtime()
        registry = TimerRegistry(runtime)
        gid = GrainId("ErrorTimerGrain", "e1")
        await runtime.invoke(gid, "ping")

        registry.register_timer(gid, "bad_tick", interval=0.03, due_time=0.0)
        await asyncio.sleep(0.1)

        # Grain should still be alive
        result = await runtime.invoke(gid, "ping")
        assert result == "pong"

        registry.unregister_all(gid)
        await runtime.stop()
