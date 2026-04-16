# Task 07: Grain Base Class -- `Grain[TState]`

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-core-types.md](task-02-core-types.md)
- [task-03-errors.md](task-03-errors.md)
- [task-06-grain-decorator.md](task-06-grain-decorator.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Decision 11

## Description

Introduce a `Grain[TState]` generic base class to eliminate the 5-line boilerplate
that every stateful grain currently declares. This matches Orleans' `Grain<TState>`
pattern and reduces grain definitions to their essential business logic.

### Motivation

Every stateful grain currently repeats:
```python
identity: GrainId
state: CounterState
save_state: Callable[[], Awaitable[None]]
clear_state: Callable[[], Awaitable[None]]
request_deactivation: Callable[[], None]
```
Plus the imports for `Callable`, `Awaitable`, and `GrainId`. This is pure boilerplate
that adds noise and violates DRY.

### Files to create/modify

**Create:**
- `pyleans/pyleans/grain_base.py` -- `Grain[TState]` generic base class

**Modify:**
- `pyleans/pyleans/grain.py` -- `@grain` decorator: infer `state_type` from generic
  type argument when grain inherits `Grain[TState]`
- `pyleans/pyleans/server/runtime.py` -- verify compatibility (binding mechanism unchanged)
- `counter-app/counter_app/counter_grain.py` -- inherit `Grain[CounterState]`, remove boilerplate
- `pyleans/pyleans/server/string_cache_grain.py` -- inherit `Grain[StringCacheState]`, remove boilerplate
- Tests for all modified files

### Design

```python
# pyleans/pyleans/grain_base.py
from typing import Generic, TypeVar
from pyleans.identity import GrainId
from pyleans.errors import GrainActivationError

TState = TypeVar("TState")

class Grain(Generic[TState]):
    """Base class for grains. Provides runtime-bound attributes.

    Only one type parameter (state), since grain keys are always strings.
    """
    identity: GrainId
    state: TState

    async def save_state(self) -> None:
        """Persist current state. Overridden by runtime during activation."""
        raise GrainActivationError("save_state not bound -- grain not activated")

    async def clear_state(self) -> None:
        """Clear persisted state. Overridden by runtime during activation."""
        raise GrainActivationError("clear_state not bound -- grain not activated")

    def request_deactivation(self) -> None:
        """Request deactivation after current turn. Overridden by runtime during activation."""
        raise GrainActivationError("request_deactivation not bound -- grain not activated")
```

After refactoring, a stateful grain looks like:
```python
@grain(state_type=CounterState, storage="default")
class CounterGrain(Grain[CounterState]):
    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.save_state()
        return self.state.value
```

### state_type inference (optional enhancement)

The `@grain` decorator can infer `state_type` from the generic argument:
```python
# These become equivalent:
@grain(state_type=CounterState, storage="default")
class CounterGrain(Grain[CounterState]): ...

@grain(storage="default")
class CounterGrain(Grain[CounterState]): ...  # state_type inferred
```

Use `typing.get_args()` on `__orig_bases__` to extract the type argument.
Explicit `state_type` takes precedence if both are provided.

### Runtime compatibility

The runtime's `setattr()` binding in `activate_grain` stays the same. The base class
stubs exist only to provide:
1. Type-safe declarations (IDE autocomplete, mypy)
2. Clear error messages if a grain method is called before activation
3. A single authoritative location for the grain API (DRY)

### Acceptance criteria

- [ ] `Grain[TState]` base class in `pyleans/pyleans/grain_base.py`
- [ ] `identity`, `state` declared as attributes
- [ ] `save_state`, `clear_state`, `request_deactivation` as stub methods that raise before activation
- [ ] `@grain` decorator infers `state_type` from `Grain[TState]` generic argument
- [ ] Explicit `state_type` takes precedence over inferred type
- [ ] `CounterGrain` refactored to use `Grain[CounterState]`
- [ ] `StringCacheGrain` refactored to use `Grain[StringCacheState]`
- [ ] Stateless grains (e.g. `AnswerGrain`) continue to work without base class
- [ ] All existing tests pass
- [ ] New tests for: base class stubs raise before activation, generic type inference,
  precedence of explicit vs inferred state_type
- [ ] mypy passes in strict mode

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
