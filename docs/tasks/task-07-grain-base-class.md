# Task 07: Grain Base Class -- `Grain[TState]`

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-core-types.md](task-02-core-types.md)
- [task-03-errors.md](task-03-errors.md)
- [task-06-grain-decorator.md](task-06-grain-decorator.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Grain Base Class

## Description

Introduce a `Grain[TState]` generic base class to eliminate the 5-line boilerplate
that every stateful grain currently declares. This matches Orleans' `Grain<TState>`
pattern and reduces grain definitions to their essential business logic.

### Motivation

Every stateful grain currently repeats:
```python
identity: GrainId
state: CounterState
write_state: Callable[[], Awaitable[None]]
clear_state: Callable[[], Awaitable[None]]
deactivate_on_idle: Callable[[], None]
```
Plus the imports for `Callable`, `Awaitable`, and `GrainId`. This is pure boilerplate
that adds noise and violates DRY.

### Files to create/modify

**Create:**
- `src/pyleans/pyleans/grain_base.py` -- `Grain[TState]` generic base class

**Modify:**
- `src/pyleans/pyleans/grain.py` -- `@grain` decorator: infer `state_type` from generic
  type argument when grain inherits `Grain[TState]`
- `src/pyleans/pyleans/server/runtime.py` -- verify compatibility (binding mechanism unchanged)
- `src/counter_app/counter_grain.py` -- inherit `Grain[CounterState]`, remove boilerplate
- `src/pyleans/pyleans/server/string_cache_grain.py` -- inherit `Grain[StringCacheState]`, remove boilerplate
- Tests for all modified files

### Design

```python
# src/pyleans/pyleans/grain_base.py
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

    async def write_state(self) -> None:
        """Persist current state. Overridden by runtime during activation."""
        raise GrainActivationError("write_state not bound -- grain not activated")

    async def clear_state(self) -> None:
        """Clear persisted state. Overridden by runtime during activation."""
        raise GrainActivationError("clear_state not bound -- grain not activated")

    def deactivate_on_idle(self) -> None:
        """Request deactivation after current turn. Overridden by runtime during activation."""
        raise GrainActivationError("deactivate_on_idle not bound -- grain not activated")
```

After refactoring, a stateful grain looks like:
```python
@grain(state_type=CounterState, storage="default")
class CounterGrain(Grain[CounterState]):
    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.write_state()
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

- [x] `Grain[TState]` base class in `src/pyleans/pyleans/grain_base.py`
- [x] `identity`, `state` declared as attributes
- [x] `write_state`, `clear_state`, `deactivate_on_idle` as stub methods that raise before activation
- [x] `@grain` decorator infers `state_type` from `Grain[TState]` generic argument
- [x] Explicit `state_type` takes precedence over inferred type
- [x] `CounterGrain` refactored to use `Grain[CounterState]`
- [x] `StringCacheGrain` refactored to use `Grain[StringCacheState]`
- [x] Stateless grains (e.g. `AnswerGrain`) continue to work without base class
- [x] All existing tests pass (349 tests)
- [x] New tests for: base class stubs raise before activation, generic type inference,
  precedence of explicit vs inferred state_type
- [x] mypy passes in strict mode (production code clean; 11 pre-existing test-only issues remain)

## Findings of code review

No issues found. Code follows SOLID (single responsibility: base class owns attributes,
decorator owns registration, runtime owns binding), DRY (attributes declared once in
base class), and KISS (minimal changes to existing runtime — stubs overridden by setattr).
One `# type: ignore[no-any-return]` in `_infer_state_type` is justified because
`typing.get_args` returns `tuple[Any, ...]`.

## Findings of security review

No security concerns. The base class is pure data structure and method stubs. The
`_infer_state_type` function only reads `__orig_bases__` metadata set by Python's
type system — no user input, no deserialization, no external I/O.

## Summary of implementation

### Files created
- `src/pyleans/pyleans/grain_base.py` — `Grain[TState]` generic base class (PEP 695 syntax)
- `src/pyleans/test/test_grain_base.py` — 19 tests

### Files modified
- `src/pyleans/pyleans/grain.py` — `_infer_state_type()`, `_BASE_CLASS_METHODS` exclusion set
- `src/pyleans/pyleans/__init__.py` — export `Grain`
- `src/counter_app/counter_grain.py` — inherits `Grain[CounterState]`, removed boilerplate
- `src/pyleans/pyleans/server/string_cache_grain.py` — inherits `Grain[StringCacheState]`, removed boilerplate
- `src/pyleans/test/test_runtime.py` — test grain uses `Grain[CounterState]`
- `src/pyleans/test/test_silo.py` — test grain uses `Grain[CounterState]`
- `src/pyleans/test/test_silo_management.py` — test grain uses `Grain[MgmtCounterState]`
- `src/pyleans/test/test_reference.py` — test grain uses `Grain[CounterState]`
- `src/pyleans/test/test_gateway.py` — test grain uses `Grain[GwCounterState]`

### Key decisions
- Used PEP 695 type parameter syntax (`class Grain[TState]`) per ruff UP046 rule and Python 3.12+ target.
- Stub methods raise `GrainActivationError` (not `NotImplementedError`) for clear diagnostics.
- `_infer_state_type` uses `__orig_bases__` + `get_origin`/`get_args` to extract generic arg.
- `Grain[None]` is treated as stateless (state_type = None).
- Added `_BASE_CLASS_METHODS` frozenset to exclude `write_state`, `clear_state`, `deactivate_on_idle` from `get_grain_methods()` discovery.
- Cleaned up unused `# type: ignore` comments across test files that became stale after adopting the base class.

### Deviations
- None.

### Test coverage
- 19 new tests: stubs raise before activation (5), runtime binding override (4), state_type inference (5), method exclusion (3), registration (2).
- All 349 tests pass (330 existing + 19 new).
