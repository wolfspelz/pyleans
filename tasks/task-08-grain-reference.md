# Task 08: Grain Reference (Proxy)

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-core-types.md](task-02-core-types.md)
- [task-07-grain-runtime.md](task-07-grain-runtime.md)
- [task-04-serialization.md](task-04-serialization.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Decision 1 (proxy via __getattr__)
- [orleans-grains.md](../docs/orleans-grains.md) -- Activation vs Reference
- [orleans-networking.md](../docs/orleans-networking.md) -- Grain method invocation

## Description

Implement `GrainRef` -- a proxy object that represents a grain and forwards
method calls to the runtime (local or remote).

### Files to create
- `src/pyleans/reference.py`

### Design

```python
class GrainRef:
    """
    A proxy for a grain. Calling methods on this proxy dispatches
    to the grain runtime, which activates the grain if needed and
    ensures turn-based execution.

    Usage:
        counter = grain_factory.get_grain(CounterGrain, "my-counter")
        value = await counter.get_value()    # calls CounterGrain.get_value
        await counter.increment()             # calls CounterGrain.increment
    """

    def __init__(self, grain_id: GrainId, runtime: GrainRuntime):
        self._grain_id = grain_id
        self._runtime = runtime

    @property
    def identity(self) -> GrainId:
        return self._grain_id

    def __getattr__(self, name: str):
        """
        Returns an async callable that dispatches to the grain runtime.
        """
        if name.startswith('_'):
            raise AttributeError(name)

        async def _proxy_call(*args, **kwargs):
            return await self._runtime.invoke(
                self._grain_id, name, list(args), kwargs
            )

        return _proxy_call

    def __repr__(self) -> str:
        return f"GrainRef({self._grain_id})"
```

### GrainFactory

```python
class GrainFactory:
    """
    Creates grain references. Injected into grains via DI.
    """

    def __init__(self, runtime: GrainRuntime):
        self._runtime = runtime

    def get_grain(self, grain_class: type, key: str) -> GrainRef:
        """
        Get a reference to a grain by class and key.
        Does NOT activate the grain -- activation happens on first call.
        """
        grain_type = grain_class._grain_type
        grain_id = GrainId(grain_type=grain_type, key=key)
        return GrainRef(grain_id=grain_id, runtime=self._runtime)
```

### Key properties

- GrainRef is lightweight -- just holds an ID and a reference to the runtime
- Creating a GrainRef does NOT activate the grain
- GrainRef is always valid (virtual actor: the grain "exists" conceptually)
- GrainRef can be stored, passed around, serialized (it's just an identity)
- Method calls via `__getattr__` return coroutines (must be awaited)

### Acceptance criteria

- [ ] `grain_factory.get_grain(CounterGrain, "k")` returns a GrainRef
- [ ] `await ref.method()` dispatches to the runtime
- [ ] Private attributes (`_xxx`) raise AttributeError, not proxied
- [ ] GrainRef is repr-able and inspectable
- [ ] Multiple GrainRefs to same grain_id share the same activation
- [ ] Unit tests for proxy dispatch

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Files created
- `pyleans/pyleans/reference.py` — GrainRef proxy and GrainFactory
- `pyleans/test/test_reference.py` — 13 tests
- `pyleans/test/conftest.py` — Shared FakeStorageProvider for test reuse

### Key decisions
- GrainRef uses `__getattr__` to return async callables that dispatch to the runtime.
- GrainRef implements `__eq__` and `__hash__` based on grain_id for use as dict keys.
- Private attributes (`_xxx`) raise AttributeError immediately, not proxied.
- GrainFactory does NOT activate grains — activation is deferred to first call.

### Deviations
- Removed `pyleans/test/__init__.py` to allow conftest.py to be importable.

### Test coverage
- 13 tests: identity, repr, private attribute rejection, equality/hashing, proxy dispatch (stateless and stateful), multiple refs sharing activation, factory creation/non-activation/dispatch/different keys.