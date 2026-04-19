"""Numeric-stage silo lifecycle engine.

Every silo subsystem subscribes itself to a numeric stage. Startup
runs stages in ascending order; shutdown runs in descending order.
Participants registered at the same stage run concurrently — the
stage completes when all participants for that stage have finished.

Ordered stages are what make "no client call accepted before cluster
join + directory prime" enforceable. The pattern mirrors Orleans §8
so operators used to that model have no surprises.

If a participant raises during :meth:`SiloLifecycle.start`, the
engine cancels remaining participants in that stage, then runs
:meth:`on_stop` on every stage that already completed, in descending
order. Shutdown itself never interrupts on error — it logs and
continues, because a silo trying to shut down must not hang.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Final, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_DEFAULT_STAGE_START_TIMEOUT: Final[float] = 30.0
_DEFAULT_STAGE_STOP_TIMEOUT: Final[float] = 5.0


class LifecycleStage(IntEnum):
    """Canonical silo lifecycle stages; values spaced for user insertion."""

    FIRST = -1_000_000
    RUNTIME_INITIALIZE = 2_000
    RUNTIME_SERVICES = 4_000
    RUNTIME_STORAGE = 6_000
    RUNTIME_CLUSTER = 7_000
    RUNTIME_GRAIN_SERVICES = 8_000
    APPLICATION_SERVICES = 10_000
    BECOME_ACTIVE = 19_999
    ACTIVE = 20_000
    LAST = 1_000_000


@runtime_checkable
class LifecycleParticipant(Protocol):
    """Any object with ``on_start(stage)`` and ``on_stop(stage)`` coroutines."""

    async def on_start(self, stage: int) -> None: ...

    async def on_stop(self, stage: int) -> None: ...


class StartupAbortedError(Exception):
    """Raised when a participant's :meth:`on_start` raises.

    The engine has already run :meth:`on_stop` on every stage that
    completed before the failure; the caller need only propagate.
    """


ParticipantFn = Callable[[int], Awaitable[None]]


@dataclass(frozen=True)
class _Participant:
    """Wraps either a :class:`LifecycleParticipant` or a pair of hook callables."""

    on_start: ParticipantFn
    on_stop: ParticipantFn
    label: str

    @classmethod
    def from_participant(cls, p: LifecycleParticipant, label: str) -> _Participant:
        return cls(on_start=p.on_start, on_stop=p.on_stop, label=label)

    @classmethod
    def from_hooks(
        cls,
        on_start: ParticipantFn,
        on_stop: ParticipantFn,
        label: str,
    ) -> _Participant:
        return cls(on_start=on_start, on_stop=on_stop, label=label)


class SiloLifecycle:
    """Drive per-stage startup / shutdown across subscribed participants."""

    def __init__(
        self,
        *,
        stage_start_timeout: float = _DEFAULT_STAGE_START_TIMEOUT,
        stage_stop_timeout: float = _DEFAULT_STAGE_STOP_TIMEOUT,
    ) -> None:
        if stage_start_timeout <= 0:
            raise ValueError(
                f"stage_start_timeout must be > 0, got {stage_start_timeout!r}",
            )
        if stage_stop_timeout <= 0:
            raise ValueError(
                f"stage_stop_timeout must be > 0, got {stage_stop_timeout!r}",
            )
        self._stage_start_timeout = stage_start_timeout
        self._stage_stop_timeout = stage_stop_timeout
        self._participants: dict[int, list[_Participant]] = defaultdict(list)
        self._completed_stages: list[int] = []
        self._started = False
        self._stopped = False

    @property
    def completed_stages(self) -> list[int]:
        return list(self._completed_stages)

    def subscribe(
        self, stage: int, participant: LifecycleParticipant, *, label: str | None = None
    ) -> None:
        """Register ``participant`` at ``stage``."""
        if self._started:
            raise RuntimeError("cannot subscribe after lifecycle.start() has been called")
        chosen_label = label or type(participant).__name__
        self._participants[int(stage)].append(
            _Participant.from_participant(participant, chosen_label)
        )

    def subscribe_hooks(
        self,
        stage: int,
        *,
        on_start: ParticipantFn,
        on_stop: ParticipantFn,
        label: str,
    ) -> None:
        """Register explicit start/stop callables at ``stage``."""
        if self._started:
            raise RuntimeError("cannot subscribe after lifecycle.start() has been called")
        self._participants[int(stage)].append(_Participant.from_hooks(on_start, on_stop, label))

    async def start(self) -> None:
        """Run every stage's ``on_start`` in ascending stage order.

        Participants within one stage run concurrently. If any raises,
        the engine reverses through the completed stages, running
        ``on_stop`` on each, then re-raises :class:`StartupAbortedError`.
        """
        if self._started:
            raise RuntimeError("SiloLifecycle already started")
        self._started = True
        for stage in sorted(self._participants):
            logger.info("lifecycle: starting stage %d", stage)
            try:
                await asyncio.wait_for(
                    self._run_stage_start(stage),
                    timeout=self._stage_start_timeout,
                )
            except Exception as exc:
                logger.error("lifecycle: stage %d failed: %r", stage, exc)
                await self._rollback_from_failure()
                if isinstance(exc, StartupAbortedError):
                    raise
                raise StartupAbortedError(f"stage {stage} start failed: {exc}") from exc
            self._completed_stages.append(stage)
            logger.info("lifecycle: stage %d complete", stage)

    async def stop(self) -> None:
        """Run ``on_stop`` on every completed stage in descending order.

        Never raises — each participant's exception is logged and the
        engine continues. Stage-level timeouts force continuation.
        """
        if self._stopped:
            return
        self._stopped = True
        for stage in reversed(self._completed_stages):
            logger.info("lifecycle: stopping stage %d", stage)
            try:
                await asyncio.wait_for(
                    self._run_stage_stop(stage),
                    timeout=self._stage_stop_timeout,
                )
            except TimeoutError:
                logger.error(
                    "lifecycle: stage %d stop timed out after %ss; forcing continue",
                    stage,
                    self._stage_stop_timeout,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("lifecycle: stage %d stop raised: %r; continuing", stage, exc)
        self._completed_stages.clear()

    async def _run_stage_start(self, stage: int) -> None:
        participants = self._participants[stage]
        if not participants:
            return
        await asyncio.gather(
            *(self._call_on_start(p, stage) for p in participants),
        )

    async def _run_stage_stop(self, stage: int) -> None:
        participants = self._participants[stage]
        if not participants:
            return
        # Run every stop; collect exceptions, never cancel.
        results = await asyncio.gather(
            *(self._call_on_stop(p, stage) for p in participants),
            return_exceptions=True,
        )
        for participant, result in zip(participants, results, strict=True):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.error(
                    "lifecycle: participant %s on_stop(%d) raised: %r",
                    participant.label,
                    stage,
                    result,
                )

    async def _call_on_start(self, p: _Participant, stage: int) -> None:
        try:
            await p.on_start(stage)
        except Exception as exc:
            logger.error(
                "lifecycle: participant %s on_start(%d) raised: %r",
                p.label,
                stage,
                exc,
            )
            raise

    async def _call_on_stop(self, p: _Participant, stage: int) -> None:
        await p.on_stop(stage)

    async def _rollback_from_failure(self) -> None:
        """Reverse through stages that already ran on_start and stop them."""
        for stage in reversed(self._completed_stages):
            logger.info("lifecycle: rolling back stage %d", stage)
            try:
                await asyncio.wait_for(
                    self._run_stage_stop(stage),
                    timeout=self._stage_stop_timeout,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("lifecycle: rollback of stage %d raised: %r", stage, exc)
        self._completed_stages.clear()
        self._stopped = True
