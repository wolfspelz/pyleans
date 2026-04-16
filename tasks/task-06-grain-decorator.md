# Task 06: Grain Decorator and State Management

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-core-types.md](task-02-core-types.md)
- [task-04-serialization.md](task-04-serialization.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Decision 1, Decision 7
- [orleans-grains.md](../docs/orleans-grains.md) -- What is a Grain, Grain Persistence

## Description

Implement the `@grain` decorator that marks a Python class as a virtual actor.

### Files to create
- `pyleans/pyleans/grain.py`

### The @grain decorator

```python
def grain(cls=None, *, state_type: type | None = None, storage: str = "default"):
    """
    Marks a class as a grain (virtual actor).

    Args:
        state_type: Optional dataclass type for persistent state.
                    If None, the grain is stateless (in-memory only).
        storage: Name of the storage provider to use.
    """
```

The decorator:
1. Records metadata on the class: `_grain_type` (class name), `_state_type`, `_storage_name`
2. Registers the class in a global grain registry (`dict[str, type]`)
3. Discovers public async methods as the grain's callable interface
4. Adds `identity` and `state` properties that the runtime will populate
5. Does NOT modify `__init__` -- DI handles that

### Usage

```python
@grain(state_type=CounterState, storage="default")
class CounterGrain:
    @inject
    def __init__(self, logger: Logger = Provide[SiloContainer.logger]):
        self.logger = logger

    async def on_activate(self):
        """Called by runtime after state is loaded."""
        self.logger.info(f"Counter {self.identity.key} activated")

    async def on_deactivate(self):
        """Called by runtime before deactivation."""
        pass

    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.write_state()  # explicit save, like Orleans
        return self.state.value
```

### Grain registry

```python
_grain_registry: dict[str, type] = {}

def get_grain_class(grain_type: str) -> type:
    """Look up a registered grain class by type name."""

def get_grain_methods(grain_class: type) -> dict[str, Callable]:
    """Return dict of method_name -> method for all public async methods."""
```

### Properties set by runtime

- `self.identity: GrainId` -- set by runtime before `on_activate`
- `self.state: T` -- loaded from storage before `on_activate` (if state_type configured)
- `self.write_state()` -- async method to persist current state
- `self.clear_state()` -- async method to clear persisted state

### Acceptance criteria

- [x] `@grain` usable with and without arguments: `@grain` and `@grain(state_type=X)`
- [x] Grain class registered in global registry
- [x] Public async methods discoverable via `get_grain_methods()`
- [x] Private methods (starting with `_`) excluded from interface
- [x] `on_activate` and `on_deactivate` recognized as lifecycle hooks, not interface methods
- [x] Metadata accessible: `cls._grain_type`, `cls._state_type`, `cls._storage_name`
- [x] Unit tests for decorator, registry, method discovery

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Files created
- `pyleans/pyleans/grain.py` — `@grain` decorator, registry, method discovery
- `pyleans/test/test_grain.py` — 19 tests

### Key decisions
- Decorator stores metadata as class attributes (`_grain_type`, `_state_type`, `_storage_name`).
- `get_grain_methods` uses `inspect.isfunction` + `asyncio.iscoroutinefunction` to discover only public async methods.
- `LIFECYCLE_METHODS` is a frozenset for O(1) lookups.
- `identity`, `state`, `write_state`, `clear_state` are NOT added by the decorator — they will be set by the runtime (Task 08).

### Deviations
- None.

### Test coverage
- 19 tests: decorator with/without args, registry lookup, not-found error, method discovery (public/private/dunder/sync/lifecycle exclusions), metadata for stateful/stateless grains.


**Resolved**: Grain Base Class (Decision 11) implemented in [task-07-grain-base-class.md](task-07-grain-base-class.md).