# Task 16: Counter Grain

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-06-grain-decorator.md](task-06-grain-decorator.md)
- [task-07-grain-base-class.md](task-07-grain-base-class.md)
- [task-15-silo.md](task-15-silo.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Phase 1 milestone

## Description

Implement a `CounterGrain` -- the first sample grain. Demonstrates state
persistence, the @grain decorator, and DI.

### Files to create
- `counter-app/counter_app/counter_grain.py` (one grain per file)

### Design

```python
from dataclasses import dataclass
from typing import Any

from dependency_injector.wiring import inject, Provide
from pyleans import grain
from pyleans.server.container import PyleansContainer
from pyleans.server.silo_management import SiloManagement

@dataclass
class CounterState:
    value: int = 0

@grain(state_type=CounterState, storage="default")
class CounterGrain:
    @inject
    def __init__(
        self,
        silo_mgmt: SiloManagement = Provide[PyleansContainer.silo_management],
    ):
        self._silo_mgmt = silo_mgmt

    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.save_state()
        return self.state.value

    async def set_value(self, value: int) -> None:
        self.state.value = value
        await self.save_state()

    async def reset(self) -> None:
        self.state.value = 0
        await self.save_state()

    async def get_silo_info(self) -> dict[str, Any]:
        return self._silo_mgmt.get_info()
```

Note: `identity`, `state`, `save_state`, `clear_state` are still runtime-bound
(per-grain-instance). Singleton services like `SiloManagement` come through DI.

### Acceptance criteria

- [x] `CounterGrain` registered via `@grain`
- [x] `get_value` returns current count
- [x] `increment` increases and persists
- [x] `set_value` sets and persists
- [x] State survives grain deactivation and reactivation (read from file)
- [x] Multiple counter instances (different keys) are independent
- [x] `SiloManagement` injected via `@inject` + `Provide[...]` in constructor
- [x] `get_silo_info()` returns silo metadata via DI-injected service

## Findings of code review

No issues found. The grain is simple, follows the decorator pattern correctly, and all methods persist state via `save_state()`. Type ignore comments are necessary because `state` and `save_state` are dynamically bound by the runtime.

## Findings of security review

No issues found. The CounterGrain is pure application logic with no system boundary interactions. State persistence is delegated to the storage provider which already handles path sanitization and etag validation.

## Summary of implementation

### Files created/modified
- **Created**: `counter-app/counter_app/counter_grain.py` — CounterGrain with CounterState (renamed from `grains.py`)
- **Created**: `counter-app/test/test_counter_grain.py` — 17 tests across 8 test classes
- **Modified**: `counter-app/pyproject.toml` — Added hatch wheel packages config and asyncio_mode=auto

### Key implementation decisions
- Placed grain in `counter-app/counter/grains.py` (existing package structure) instead of `examples/counter-app/grains.py` (task spec path doesn't match project layout).
- Used `# type: ignore[attr-defined]` for `self.state`, `self.save_state` since these are dynamically bound by the runtime during activation.
- Tests use the full Silo with fake providers rather than testing the grain in isolation, ensuring end-to-end correctness.

### Deviations from original design
- Omitted the empty `on_activate` method — it adds no value since state is loaded automatically.
- File location changed from task spec `examples/counter-app/grains.py` to `counter-app/counter_app/counter_grain.py` — one grain per file convention.

### Test coverage summary
- 17 tests: registration (5), state defaults (2), get_value (1), increment (2), set_value (2), reset (1), state survival through deactivation and silo restart (2), multiple independent instances (1), concurrent counters (1).

## Open: Grain Base Class Refactoring (Decision 11)

Per [pyleans-plan.md Decision 11](../docs/pyleans-plan.md), refactor `CounterGrain` to
inherit from `Grain[CounterState]` instead of declaring runtime-bound attributes manually.
Remove the 5 boilerplate attribute declarations and the `Callable`/`Awaitable` imports.
Also refactor `AnswerGrain` — it can remain a plain decorated class (stateless, no
runtime-bound attributes) or optionally inherit `Grain[None]` if it needs `identity`.
Similarly update `StringCacheGrain` in `pyleans/server/`.