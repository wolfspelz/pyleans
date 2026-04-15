# Task 12: Grain Timers

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-07-grain-runtime.md](task-07-grain-runtime.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Phase 1 item 11
- [orleans-grains.md](../docs/orleans-grains.md) -- Timers and Reminders

## Description

Implement in-grain periodic timers. Timers are non-durable (lost on
deactivation), high-frequency callbacks. They respect the turn-based model --
timer callbacks are enqueued like regular grain method calls.

### Files to create
- `src/pyleans/server/timer.py`

### Design

```python
@dataclass
class GrainTimer:
    id: str
    callback: Callable[[], Awaitable[None]]
    interval: float          # seconds
    due_time: float          # initial delay in seconds
    _task: asyncio.Task | None = None

class TimerRegistry:
    """
    Manages timers for grain activations. Injected into grains via DI.
    Timer callbacks are dispatched through the grain's inbox to maintain
    turn-based execution guarantees.
    """

    def register_timer(self, grain_id: GrainId, callback_name: str,
                       interval: float, due_time: float = 0.0) -> str:
        """
        Register a periodic timer.
        Returns a timer ID for cancellation.

        The callback_name is a method on the grain. It will be invoked
        through the grain's inbox (turn-based, not re-entrant).
        """

    def unregister_timer(self, timer_id: str) -> None:
        """Cancel a timer."""

    def unregister_all(self, grain_id: GrainId) -> None:
        """Cancel all timers for a grain (called on deactivation)."""
```

### Timer execution

Timers do NOT call the grain method directly. Instead, they enqueue a
message in the grain's inbox:

```python
async def _timer_loop(self, timer: GrainTimer, grain_id: GrainId):
    await asyncio.sleep(timer.due_time)
    while True:
        # Dispatch through runtime.invoke to maintain turn-based execution
        await self._runtime.invoke(grain_id, timer.callback_name, [], {})
        await asyncio.sleep(timer.interval)
```

This ensures timer callbacks never run concurrently with other grain methods.

### Grain usage

```python
@grain
class HeartbeatGrain:
    @inject
    def __init__(self, timers: TimerRegistry = Provide[...]):
        self.timers = timers

    async def on_activate(self):
        self.timers.register_timer(
            self.identity, "_on_heartbeat",
            interval=30.0, due_time=5.0
        )

    async def _on_heartbeat(self):
        # This runs as a turn-based grain call
        print(f"heartbeat at {time.time()}")

    async def on_deactivate(self):
        self.timers.unregister_all(self.identity)
```

### Acceptance criteria

- [ ] Timer fires after due_time, then every interval
- [ ] Timer callback executes through grain inbox (turn-based)
- [ ] Timer cancelled via `unregister_timer`
- [ ] All timers cancelled on grain deactivation
- [ ] Timer errors logged but don't crash the grain
- [ ] Unit tests with fast intervals

## Summary of implementation

### Files created
- `pyleans/pyleans/server/timer.py` — GrainTimer, TimerRegistry
- `pyleans/test/test_timer.py` — 7 tests

### Key decisions
- Timer callbacks dispatched through runtime.invoke to maintain turn-based execution.
- Timer loop catches and logs errors without crashing (grain stays alive).
- `unregister_all` called on grain deactivation to clean up.
- Timer IDs are UUID4 strings.

### Deviations
- None.

### Test coverage
- 7 tests: register/unregister/unregister_all, timer fires at interval, due_time respected, error in callback doesn't crash grain.