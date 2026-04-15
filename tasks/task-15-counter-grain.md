# Task 15: Counter Grain

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-06-grain-decorator.md](task-06-grain-decorator.md)
- [task-14-silo.md](task-14-silo.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Phase 1 milestone

## Description

Implement a `CounterGrain` -- the first sample grain. Demonstrates state
persistence, the @grain decorator, and DI.

### Files to create
- `examples/counter-app/grains.py`

### Design

```python
from dataclasses import dataclass
from pyleans import grain

@dataclass
class CounterState:
    value: int = 0

@grain(state_type=CounterState, storage="default")
class CounterGrain:
    async def on_activate(self):
        pass  # state already loaded

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
```

### Acceptance criteria

- [ ] `CounterGrain` registered via `@grain`
- [ ] `get_value` returns current count
- [ ] `increment` increases and persists
- [ ] `set_value` sets and persists
- [ ] State survives grain deactivation and reactivation (read from file)
- [ ] Multiple counter instances (different keys) are independent

## Summary of implementation
_To be filled when task is complete._