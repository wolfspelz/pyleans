"""Tests for :mod:`pyleans.server.lifecycle`.

Covers: ascending start / descending stop ordering, concurrent
execution within a stage, startup-failure rollback of completed
stages, best-effort shutdown that keeps going past exceptions,
stage-level timeouts on both start and stop, and subscription
validation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from pyleans.server.lifecycle import (
    LifecycleStage,
    SiloLifecycle,
    StartupAbortedError,
)


@dataclass
class Recorder:
    log: list[tuple[str, int, str]] = field(default_factory=list)

    def participant(
        self,
        label: str,
        *,
        start_sleep: float = 0.0,
        stop_sleep: float = 0.0,
        raise_on_start: Exception | None = None,
        raise_on_stop: Exception | None = None,
    ) -> object:
        log = self.log

        class _Participant:
            async def on_start(self_, stage: int) -> None:  # noqa: N805
                del self_
                if start_sleep > 0:
                    await asyncio.sleep(start_sleep)
                log.append(("start", stage, label))
                if raise_on_start is not None:
                    raise raise_on_start

            async def on_stop(self_, stage: int) -> None:  # noqa: N805
                del self_
                if stop_sleep > 0:
                    await asyncio.sleep(stop_sleep)
                log.append(("stop", stage, label))
                if raise_on_stop is not None:
                    raise raise_on_stop

        return _Participant()


class TestStageOrdering:
    async def test_start_runs_in_ascending_stage_order(self) -> None:
        # Arrange
        rec = Recorder()
        lifecycle = SiloLifecycle()
        lifecycle.subscribe(LifecycleStage.ACTIVE, rec.participant("active"))
        lifecycle.subscribe(LifecycleStage.RUNTIME_SERVICES, rec.participant("services"))
        lifecycle.subscribe(LifecycleStage.RUNTIME_CLUSTER, rec.participant("cluster"))

        # Act
        await lifecycle.start()
        await lifecycle.stop()

        # Assert - starts go up, stops go down
        start_events = [label for kind, _, label in rec.log if kind == "start"]
        stop_events = [label for kind, _, label in rec.log if kind == "stop"]
        assert start_events == ["services", "cluster", "active"]
        assert stop_events == ["active", "cluster", "services"]

    async def test_completed_stages_tracked(self) -> None:
        # Arrange
        lifecycle = SiloLifecycle()
        lifecycle.subscribe(LifecycleStage.RUNTIME_STORAGE, Recorder().participant("s"))
        lifecycle.subscribe(LifecycleStage.ACTIVE, Recorder().participant("a"))

        # Act
        await lifecycle.start()

        # Assert
        assert lifecycle.completed_stages == [
            LifecycleStage.RUNTIME_STORAGE,
            LifecycleStage.ACTIVE,
        ]

        # Cleanup
        await lifecycle.stop()


class TestIntraStageConcurrency:
    async def test_same_stage_participants_run_concurrently(self) -> None:
        # Arrange - two 50ms sleeps in same stage; total should be ~50ms, not ~100ms
        rec = Recorder()
        lifecycle = SiloLifecycle()
        lifecycle.subscribe(
            LifecycleStage.RUNTIME_CLUSTER,
            rec.participant("p1", start_sleep=0.05),
        )
        lifecycle.subscribe(
            LifecycleStage.RUNTIME_CLUSTER,
            rec.participant("p2", start_sleep=0.05),
        )
        loop = asyncio.get_running_loop()

        # Act
        started_at = loop.time()
        await lifecycle.start()
        elapsed = loop.time() - started_at

        # Assert
        assert elapsed < 0.09  # well under 2*0.05
        await lifecycle.stop()


class TestStartupFailure:
    async def test_failure_rolls_back_completed_stages(self) -> None:
        # Arrange
        rec = Recorder()
        lifecycle = SiloLifecycle()
        lifecycle.subscribe(LifecycleStage.RUNTIME_STORAGE, rec.participant("s"))
        lifecycle.subscribe(
            LifecycleStage.RUNTIME_CLUSTER,
            rec.participant("c", raise_on_start=RuntimeError("cluster failed")),
        )
        lifecycle.subscribe(LifecycleStage.ACTIVE, rec.participant("a"))

        # Act / Assert
        with pytest.raises(StartupAbortedError):
            await lifecycle.start()
        # Storage started and was rolled back; active never ran.
        assert ("start", LifecycleStage.RUNTIME_STORAGE, "s") in rec.log
        assert ("stop", LifecycleStage.RUNTIME_STORAGE, "s") in rec.log
        assert not any(label == "a" for _, _, label in rec.log)

    async def test_subscribe_rejected_after_start(self) -> None:
        # Arrange
        lifecycle = SiloLifecycle()
        lifecycle.subscribe(LifecycleStage.RUNTIME_STORAGE, Recorder().participant("s"))
        await lifecycle.start()

        # Act / Assert
        with pytest.raises(RuntimeError, match=r"after lifecycle\.start"):
            lifecycle.subscribe(LifecycleStage.ACTIVE, Recorder().participant("x"))

        await lifecycle.stop()


class TestShutdownRobustness:
    async def test_stop_continues_despite_participant_exception(self) -> None:
        # Arrange
        rec = Recorder()
        lifecycle = SiloLifecycle()
        lifecycle.subscribe(
            LifecycleStage.RUNTIME_STORAGE,
            rec.participant("s", raise_on_stop=RuntimeError("stop boom")),
        )
        lifecycle.subscribe(LifecycleStage.ACTIVE, rec.participant("a"))
        await lifecycle.start()

        # Act (must not raise)
        await lifecycle.stop()

        # Assert - both stages' stops recorded (storage before exception, active completed)
        assert ("stop", LifecycleStage.ACTIVE, "a") in rec.log
        # Storage stop was invoked (exception swallowed by engine)
        assert ("stop", LifecycleStage.RUNTIME_STORAGE, "s") in rec.log

    async def test_stop_is_idempotent(self) -> None:
        # Arrange
        lifecycle = SiloLifecycle()
        lifecycle.subscribe(LifecycleStage.ACTIVE, Recorder().participant("a"))
        await lifecycle.start()
        await lifecycle.stop()

        # Act (must not raise)
        await lifecycle.stop()


class TestTimeouts:
    async def test_start_timeout_aborts_stage(self) -> None:
        # Arrange - participant sleeps longer than the per-stage timeout
        lifecycle = SiloLifecycle(stage_start_timeout=0.05)
        lifecycle.subscribe(
            LifecycleStage.ACTIVE,
            Recorder().participant("slow", start_sleep=1.0),
        )

        # Act / Assert
        with pytest.raises(StartupAbortedError):
            await lifecycle.start()

    async def test_stop_timeout_forces_continuation(self) -> None:
        # Arrange
        rec = Recorder()
        lifecycle = SiloLifecycle(stage_stop_timeout=0.05)
        lifecycle.subscribe(LifecycleStage.RUNTIME_STORAGE, rec.participant("s"))
        lifecycle.subscribe(
            LifecycleStage.ACTIVE,
            rec.participant("slow", stop_sleep=1.0),
        )
        await lifecycle.start()

        # Act (must return without hanging even though ACTIVE.on_stop blocks)
        await lifecycle.stop()

        # Assert - storage stop still ran
        assert ("stop", LifecycleStage.RUNTIME_STORAGE, "s") in rec.log


class TestSubscribeHooks:
    async def test_hooks_driven_lifecycle(self) -> None:
        # Arrange - a participant expressed as callables rather than an object
        log: list[str] = []

        async def start(stage: int) -> None:
            log.append(f"start-{stage}")

        async def stop(stage: int) -> None:
            log.append(f"stop-{stage}")

        lifecycle = SiloLifecycle()
        lifecycle.subscribe_hooks(
            LifecycleStage.APPLICATION_SERVICES,
            on_start=start,
            on_stop=stop,
            label="hooks-participant",
        )

        # Act
        await lifecycle.start()
        await lifecycle.stop()

        # Assert
        assert log == [
            f"start-{LifecycleStage.APPLICATION_SERVICES.value}",
            f"stop-{LifecycleStage.APPLICATION_SERVICES.value}",
        ]


class TestConstruction:
    def test_rejects_non_positive_start_timeout(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="stage_start_timeout"):
            SiloLifecycle(stage_start_timeout=0)

    def test_rejects_non_positive_stop_timeout(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="stage_stop_timeout"):
            SiloLifecycle(stage_stop_timeout=0)
