# Task 02-23: Grain `read_state()` and Counter `reload()`

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-07-grain-base-class.md](task-01-07-grain-base-class.md)
- [task-01-08-grain-runtime.md](task-01-08-grain-runtime.md)
- [task-01-18-counter-grain.md](task-01-18-counter-grain.md)

## References
- [adr-grain-base-class](../adr/adr-grain-base-class.md) -- lists `read_state()` as a runtime-bound attribute.
- [adr-state-declaration](../adr/adr-state-declaration.md) -- single storage binding per grain class.
- [domain/0001-grains-are-an-active-cache](../domain/0001-grains-are-an-active-cache.md) -- the invariant `read_state()` deliberately lets a grain override.

## Description

An activation reads its state from storage exactly once, at activation time. After that, `grain.state` is the authoritative cache and the storage provider is never consulted again automatically (see [domain/0001-grains-are-an-active-cache](../domain/0001-grains-are-an-active-cache.md)). Some grains occasionally need to reconcile with out-of-band storage changes: an operator edit, a migration job, a sibling process that writes to the same storage row. This task adds `read_state()` to the `Grain[TState]` surface as the explicit escape hatch.

The sample `CounterGrain` gains a `reload()` method that calls `read_state()` and returns the freshly-loaded value — a minimal demonstration that a grain can re-sync with storage on demand.

### Motivation

Today the grain base class exposes `write_state()` and `clear_state()` but offers no supported way to pull storage contents back into memory. The only workaround is to force a deactivation (`deactivate_on_idle()`) and wait for a later activation. That is coarse-grained, observable to callers, and surprising -- the grain goes away. A first-class `read_state()` closes the gap without breaking the active-cache invariant: the invariant is that storage is not consulted *automatically*; an explicit call is exactly the authorised override the domain entry predicts.

### Files to create/modify

**Modify:**
- `src/pyleans/pyleans/grain_base.py` -- add `read_state()` stub that raises `GrainActivationError` before activation.
- `src/pyleans/pyleans/grain.py` -- add `"read_state"` to `_BASE_CLASS_METHODS` so it is excluded from the grain's callable interface.
- `src/pyleans/pyleans/server/runtime.py` -- in `_bind_state_methods`, bind a `read_state` closure that calls `storage.read(...)`, replaces `instance.state`, and refreshes `activation.etag`.
- `src/counter_app/counter_grain.py` -- add `async def reload(self) -> int` that calls `self.read_state()` and returns the current value.
- `src/pyleans/test/test_grain_base.py` -- tests for the stub, override, and method-exclusion contract.
- `src/pyleans/test/test_runtime.py` -- tests for the runtime binding: refresh-from-storage, reset-when-empty, etag-updated.
- `src/counter_app/test/test_counter_grain.py` -- tests for `reload()`: picks up an external write; resets to default when the storage row is cleared.
- Tests for all created/modified files.

No new modules.

### Design

#### API addition — `Grain[TState]`

```python
# src/pyleans/pyleans/grain_base.py (sketch)

class Grain[TState]:
    ...

    async def read_state(self) -> None:
        """Reload ``state`` from the storage provider.

        Replaces the current in-memory state with what storage currently
        holds. If the storage row no longer exists, resets state to the
        default constructed ``TState``. Refreshes the activation etag
        so the next ``write_state`` uses the storage provider's latest
        version.
        """
        raise GrainActivationError("read_state not bound -- grain not activated")
```

#### Runtime binding — mirror of the existing load-on-activate path

```python
# src/pyleans/pyleans/server/runtime.py (sketch, inside _bind_state_methods)

async def read_state() -> None:
    storage = runtime._storage_providers.get(storage_name)
    if storage is None:
        return
    state_dict, etag = await storage.read(
        activation.grain_id.grain_type,
        activation.grain_id.key,
    )
    if state_dict:
        serialized = runtime._serializer.serialize(state_dict)
        instance.state = runtime._serializer.deserialize(serialized, state_type)
    else:
        instance.state = state_type()
    activation.etag = etag
    grain_logger.debug("read_state for %s", activation.grain_id)

instance.read_state = read_state
```

Behaviour mirrors the load-on-activate path (`runtime.py:340-358`) so the in-memory state after `read_state()` is exactly what the next activation would have observed. No separate code path, no drift between the two paths.

#### Counter `reload()`

```python
# src/counter_app/counter_grain.py (sketch)

async def reload(self) -> int:
    """Re-read the persisted value and return it."""
    await self.read_state()
    self.logger.info("Counter %s reloaded -> %d", self.identity.key, self.state.value)
    return self.state.value
```

### Acceptance criteria

- [ ] Calling `grain.read_state()` before activation raises `GrainActivationError`.
- [ ] After activation, `grain.read_state()` replaces `grain.state` with whatever the storage provider currently returns; an out-of-band write visible to the same provider is observable inside the grain after the call.
- [ ] If the storage row is absent (cleared out-of-band), `read_state()` resets `grain.state` to the default-constructed `TState` and nulls the activation etag.
- [ ] After `read_state()`, a subsequent `write_state()` uses the refreshed etag and succeeds against the same storage row that `read_state()` observed.
- [ ] `read_state` is **not** exposed as a grain-interface method (excluded via `_BASE_CLASS_METHODS`), mirroring `write_state` / `clear_state`.
- [ ] `CounterGrain.reload()` returns the freshly-loaded value and logs the reconciliation at INFO level.
- [ ] Unit tests cover happy path, equivalence classes (storage present / absent), boundaries (value unchanged / changed), error cases (stub raises before activation), and edge cases (back-to-back `reload()` calls, `reload()` then `increment()` persists correctly).
- [ ] `ruff check .`, `ruff format --check .`, `pylint src` (10.00/10), `mypy .` all clean.
- [ ] All existing tests still pass.

## Findings of code review

- [x] **Stub follows the existing pattern.** `read_state()` on `Grain[TState]` raises `GrainActivationError` with a message identical in shape to `write_state` / `clear_state`, so an accidental pre-activation call surfaces the same way as its siblings.
- [x] **Runtime binding co-located with sibling bindings.** The closure lives inside `_bind_state_methods` alongside `write_state` / `clear_state`, so a future change to the binding lifecycle (e.g. swapping the serializer per activation) touches one method, not three.
- [x] **`_BASE_CLASS_METHODS` kept as a set literal.** Added `"read_state"` without introducing a helper; the frozenset is still a one-line constant and `get_grain_methods` picks up the exclusion automatically.
- [x] **Load logic parallels the activation path.** Considered extracting a `_load_state_from_storage(storage, grain_id, state_type, instance)` helper to share the read → deserialize → default-or-assign → etag dance with the activation-time load at [runtime.py:344-352](../../src/pyleans/pyleans/server/runtime.py). Chose not to: the activation path has extra responsibilities (`state_loaded` flag, `try/except` that re-raises as `GrainActivationError`) which would leak into the helper's signature. DRY weighed against YAGNI; the inline version stayed.
- [x] **Log level matches the adr-logging guideline.** `read_state for %s` is per-operation and fires no more than once per call, so DEBUG is the right bucket (same as `write_state`).
- [x] **AAA labels on every new test.** All three tests in `TestStateManagement`, the two new tests in `TestGrainBaseStubs` / `TestGrainBaseRuntimeBinding`, and the three tests in `TestReload` use explicit `# Arrange` / `# Act` / `# Assert` comments. Pre-existing tests in the same modules that lacked labels were not retrofitted (out of task scope).
- [x] **Test coverage hits every class from CLAUDE.md.** Happy path (`test_reload_picks_up_external_write`, `test_read_state_refreshes_from_storage`), equivalence classes (storage present vs absent), boundaries (state reset to default, etag refreshed), error cases (`test_read_state_raises_before_activation`), edge cases (`test_reload_then_increment_persists` — etag path after reload).
- [x] **`CounterGrain.reload()` follows the sample's style.** Imperative doc, one INFO log line terminating the method, returns the observable value; matches `increment` / `set_value` / `reset`.

## Findings of security review

- [x] **No new untrusted deserialization.** `read_state` feeds the existing `JsonSerializer` with the same `state_type` dataclass the activation path uses. No `pickle`, `eval`, or dynamic class resolution introduced.
- [x] **No new file / network operations.** The binding uses the already-wired `StorageProvider` keyed off the grain's own `(grain_type, grain_key)`; no path, host, or query is user-controlled.
- [x] **No privilege escalation.** A grain that can call `read_state()` can already call `write_state()` / `clear_state()` — the same storage surface, bound by the same decorator, at the same trust boundary.
- [x] **OCC invariant preserved.** After `read_state()`, `activation.etag` is whatever the storage provider just returned, so a subsequent `write_state()` sends the current etag as `expected_etag`. Concurrent external writes still collide via the provider's etag check; OCC semantics are unchanged.
- [x] **No state content in logs.** The debug line is `"read_state for %s", activation.grain_id` — identity only, consistent with the no-secrets rule in CLAUDE.md.
- [x] **Bounded resource use.** One `storage.read` per call, one `serialize` + `deserialize` round-trip. No queues, caches, or retry loops added.
- [x] **Codebase scan — no new surfaces.** `git diff` for this task touches only `grain_base.py`, `grain.py`, `runtime.py`, `counter_grain.py`, and three test files; no changes to storage providers, gateway, transport, or membership. Orthogonal subsystems unaffected.

## Summary of implementation

### Overview
Added `read_state()` to the `Grain[TState]` base class as the authorised escape hatch from the "grain is an active cache" invariant: a grain can now explicitly re-sync its in-memory state with whatever the storage provider currently holds, without having to deactivate. The stub on the base class raises `GrainActivationError` before activation, and the runtime binds a closure that mirrors the activation-time load — read the storage row, deserialize (or default when absent), refresh the activation etag. `read_state` is excluded from a grain's callable interface via `_BASE_CLASS_METHODS`, so it remains a grain-internal primitive rather than a remote-invocable method. The sample `CounterGrain` gained a `reload()` method that wraps `read_state()` and returns the reconciled value, demonstrating the new capability end-to-end. Eight new tests cover the happy path, the storage-empty reset, the etag refresh, and the `reload()` + `increment()` chain (OCC round-trip after an out-of-band write).

### Files created
- `docs/tasks/task-02-23-grain-read-state.md` — this task spec.

### Files modified
- `docs/adr/adr-grain-base-class.md` — listed `read_state()` alongside `write_state` / `clear_state` in the runtime-bound attributes, with a one-line pointer to [domain/0001-grains-are-an-active-cache](../domain/0001-grains-are-an-active-cache.md) explaining why the explicit read is needed.
- `docs/tasks/README.md` — added a "Phase 2 Grain API Extensions" subsection indexing this task.
- `src/pyleans/pyleans/grain_base.py` — added `async def read_state(self) -> None` stub that raises `GrainActivationError` before activation, mirroring `write_state` / `clear_state`.
- `src/pyleans/pyleans/grain.py` — added `"read_state"` to `_BASE_CLASS_METHODS` so `get_grain_methods` excludes it from a grain's callable interface.
- `src/pyleans/pyleans/server/runtime.py` — `_bind_state_methods` now binds a `read_state` closure that calls the storage provider, replaces `instance.state` with the loaded (or default) dataclass, and refreshes `activation.etag`. Bound to `instance.read_state` alongside the existing `write_state` / `clear_state` bindings.
- `src/counter_app/counter_grain.py` — added `async def reload(self) -> int` that calls `self.read_state()`, logs at INFO, and returns the current value.
- `src/pyleans/test/test_grain_base.py` — `test_read_state_raises_before_activation`, `test_read_state_can_be_overridden`, and an assertion in `test_write_state_excluded_from_methods` covering `"read_state"`.
- `src/pyleans/test/test_runtime.py` — `test_read_state_refreshes_from_storage`, `test_read_state_resets_to_default_when_storage_empty`, `test_read_state_updates_etag` in `TestStateManagement`.
- `src/counter_app/test/test_counter_grain.py` — `TestReload` with three tests (external write observable after reload, reset when storage cleared, increment persists using refreshed etag); `reload` added to `test_public_methods_discovered`'s expected set.

### Key decisions
- **Kept the load logic inline** in both the activation path and `read_state`. A shared helper would have to accommodate the activation path's `state_loaded` flag and error wrapping, so the extraction's cost outweighed its gain for two callers of ~6 lines.
- **No-op when no storage provider is bound.** A grain decorated with `@grain(storage=...)` but whose silo has no matching provider entry currently defaults state at activation; `read_state` matches that behavior by returning without touching state. Grains without storage should not reach `read_state` in normal code paths.
- **Reset-to-default when storage is empty.** If the storage row was cleared out-of-band, `read_state()` resets `state` to `state_type()` and nulls the etag — exactly what a fresh activation would observe, so the in-memory view after `read_state()` is indistinguishable from "deactivate + reactivate".
- **`reload()` lives on `CounterGrain`, not the base class.** The base-class primitive is `read_state()`; `reload()` is the sample's domain-named wrapper that also returns the value and logs at INFO. Other grains pick their own naming.

### Deviations
None.

### Test coverage
- Full fast suite: **831 passing** (823 baseline from task 02-22 + 8 new tests for this task).
- `ruff check .` clean; `ruff format --check .` clean; `pylint src` 10.00/10; `mypy .` has 66 pre-existing errors (unchanged from task 02-22's baseline) and zero new errors in the files this task touched.
- New tests by layer:
  - Base class (2): stub raises before activation; stub can be overridden. Also extended `test_write_state_excluded_from_methods` to assert `"read_state"` is excluded.
  - Runtime binding (3): refreshes from storage, resets to default when storage empty, etag updated to storage's current etag.
  - Counter sample (3): `reload()` picks up external write, resets when storage cleared, `reload` + `increment` chain persists correctly using the refreshed etag.
