"""Grain timers — non-durable periodic callbacks respecting turn-based execution."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pyleans.identity import GrainId

if TYPE_CHECKING:
    from pyleans.server.runtime import GrainRuntime

logger = logging.getLogger(__name__)


@dataclass
class GrainTimer:
    """Represents a registered periodic timer for a grain."""

    id: str
    grain_id: GrainId
    callback_name: str
    interval: float
    due_time: float
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class TimerRegistry:
    """Manages periodic timers for grain activations.

    Timer callbacks are dispatched through the grain's inbox to maintain
    turn-based execution guarantees.
    """

    def __init__(self, runtime: GrainRuntime) -> None:
        self._runtime = runtime
        self._timers: dict[str, GrainTimer] = {}
        self._grain_timers: dict[GrainId, list[str]] = {}

    def register_timer(
        self,
        grain_id: GrainId,
        callback_name: str,
        interval: float,
        due_time: float = 0.0,
    ) -> str:
        """Register a periodic timer. Returns a timer ID for cancellation."""
        timer_id = str(uuid.uuid4())
        timer = GrainTimer(
            id=timer_id,
            grain_id=grain_id,
            callback_name=callback_name,
            interval=interval,
            due_time=due_time,
        )
        timer.task = asyncio.create_task(self._timer_loop(timer))
        self._timers[timer_id] = timer
        self._grain_timers.setdefault(grain_id, []).append(timer_id)
        logger.debug(
            "Timer registered for %s: %s (interval=%.1fs)", grain_id, callback_name, interval
        )
        return timer_id

    def unregister_timer(self, timer_id: str) -> None:
        """Cancel a timer by ID."""
        timer = self._timers.pop(timer_id, None)
        if timer is None:
            return
        if timer.task is not None:
            timer.task.cancel()
        grain_ids = self._grain_timers.get(timer.grain_id, [])
        if timer_id in grain_ids:
            grain_ids.remove(timer_id)
        logger.debug("Timer unregistered: %s for %s", timer_id, timer.grain_id)

    def unregister_all(self, grain_id: GrainId) -> None:
        """Cancel all timers for a grain (called on deactivation)."""
        timer_ids = self._grain_timers.pop(grain_id, [])
        for timer_id in timer_ids:
            timer = self._timers.pop(timer_id, None)
            if timer is not None and timer.task is not None:
                timer.task.cancel()

    async def _timer_loop(self, timer: GrainTimer) -> None:
        """Timer loop: wait due_time, then invoke at interval."""
        try:
            if timer.due_time > 0:
                await asyncio.sleep(timer.due_time)
            while True:
                try:
                    logger.debug("Timer tick: %s on %s", timer.callback_name, timer.grain_id)
                    await self._runtime.invoke(timer.grain_id, timer.callback_name, [], {})
                except Exception:
                    logger.warning(
                        "Timer %s callback %s on %s raised an error",
                        timer.id,
                        timer.callback_name,
                        timer.grain_id,
                        exc_info=True,
                    )
                await asyncio.sleep(timer.interval)
        except asyncio.CancelledError:
            return
