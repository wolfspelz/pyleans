# Task 02-12: Grain Directory Port -- `IGrainDirectory` ABC and Local Adapter

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-08-grain-runtime.md](task-01-08-grain-runtime.md)

## References
- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) -- `IGrainDirectory` is one of the three subsystems (with membership and cluster transport) that jointly enforce the single-activation contract. This task introduces the port; [task-02-13](task-02-13-distributed-grain-directory.md) delivers the enforcement implementation.
- [adr-grain-directory](../adr/adr-grain-directory.md) -- consistency model and hash-ring choice.
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §6.6 pluggable directory

## Description

Before building the distributed grain directory, extract the existing in-memory directory behavior (currently a `dict[GrainId, GrainActivation]` inside `GrainRuntime`) behind an abstract `IGrainDirectory` port. This is a refactor, not a feature addition: Phase 1 behavior must remain identical after this task.

Landing the port now (before the distributed implementation) is deliberate: the port is the seam where the single-activation contract from [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) becomes testable. Every directory adapter — `LocalGrainDirectory` here, `DistributedGrainDirectory` in [task-02-13](task-02-13-distributed-grain-directory.md), future adapters — must satisfy the same `register`/`resolve_or_activate` contract; contract tests written against this ABC are reusable for all of them.

Two wins from landing this first:
1. [Task-31](task-02-13-distributed-grain-directory.md) can then drop in a `DistributedGrainDirectory` implementation without touching runtime call paths.
2. The runtime tests that rely on directory semantics become reusable contract tests for any future directory adapter (single-silo local, distributed, mock, etc.).

### Files to create/modify

**Create:**
- `src/pyleans/pyleans/cluster/directory.py` -- `IGrainDirectory` ABC, directory value types
- `src/pyleans/pyleans/server/local_directory.py` -- `LocalGrainDirectory` adapter (single-silo, in-memory)

**Modify:**
- `src/pyleans/pyleans/server/runtime.py` -- consume `IGrainDirectory` via constructor injection, replace the in-memory dict lookup with directory RPCs (all local for the `LocalGrainDirectory`)
- `src/pyleans/pyleans/server/silo.py` -- construct and inject `LocalGrainDirectory` in dev-mode default (behavior unchanged)

### Design

```python
# cluster/directory.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pyleans.identity import GrainId, SiloAddress


@dataclass(frozen=True)
class DirectoryEntry:
    grain_id: GrainId
    silo: SiloAddress
    activation_epoch: int        # monotonic counter per silo; distinguishes reactivations


class IGrainDirectory(ABC):
    """Port: which silo hosts an activation of a given GrainId, cluster-wide.

    Ownership: exactly one activation per GrainId (stateless workers excluded in
    Phase 4). The directory enforces this via register-if-absent semantics:
    register() of an already-present grain returns the EXISTING entry, not an
    error. The caller is expected to route their call to that silo.
    """

    @abstractmethod
    async def register(self, grain_id: GrainId, silo: SiloAddress) -> DirectoryEntry:
        """Register-if-absent. If already registered (possibly on a different
        silo), returns the existing entry. The caller's preferred silo wins
        only if no entry exists."""

    @abstractmethod
    async def lookup(self, grain_id: GrainId) -> DirectoryEntry | None:
        """Return the registered entry or None if not registered."""

    @abstractmethod
    async def unregister(self, grain_id: GrainId, silo: SiloAddress) -> None:
        """Remove the entry if and only if it currently maps to `silo`.
        No-op if the entry is absent or maps to a different silo."""

    @abstractmethod
    async def resolve_or_activate(
        self,
        grain_id: GrainId,
        placement: PlacementStrategy,
        caller: SiloAddress | None,
    ) -> DirectoryEntry:
        """Atomic 'find or allocate' — the grain's home is decided by placement
        if no entry exists. Returns the directory entry the caller should
        route to. This is the one method distributed implementations must be
        especially careful about (see task-02-13)."""
```

### `LocalGrainDirectory`

Single-silo implementation:

```python
class LocalGrainDirectory(IGrainDirectory):
    def __init__(self, local_silo: SiloAddress) -> None:
        self._local = local_silo
        self._entries: dict[GrainId, DirectoryEntry] = {}
        self._epoch = 0

    async def register(self, grain_id, silo):
        existing = self._entries.get(grain_id)
        if existing is not None:
            return existing
        self._epoch += 1
        entry = DirectoryEntry(grain_id, silo, self._epoch)
        self._entries[grain_id] = entry
        return entry

    async def lookup(self, grain_id):
        return self._entries.get(grain_id)

    async def unregister(self, grain_id, silo):
        entry = self._entries.get(grain_id)
        if entry is not None and entry.silo == silo:
            del self._entries[grain_id]

    async def resolve_or_activate(self, grain_id, placement, caller):
        existing = self._entries.get(grain_id)
        if existing is not None:
            return existing
        # Local mode: always self-assign. Placement strategy is consulted
        # for completeness (matches distributed semantics) but the only
        # active silo is ourselves.
        chosen = placement.pick_silo(grain_id, caller, [self._local])
        assert chosen == self._local
        return await self.register(grain_id, self._local)
```

### Runtime integration

`GrainRuntime.invoke()` currently has this shape (simplified):

```python
if grain_id not in self._activations:
    await self.activate_grain(grain_id)
activation = self._activations[grain_id]
await activation.inbox.put(...)
```

After this task:

```python
entry = await self._directory.resolve_or_activate(grain_id, placement, caller=self._local_silo)
if entry.silo != self._local_silo:
    return await self._remote_invoke(entry, ...)     # stub — wired up in task-02-16
if grain_id not in self._activations:
    await self.activate_grain(grain_id)
...
```

In Phase 2 dev mode, `entry.silo` is always the local silo and the remote branch is dead code. That is fine -- it is the seam that task-02-16 wires into `TcpClusterTransport.send_request`. Landing this seam now means task-02-16 can be reviewed as a small delta rather than a runtime rewrite.

### Deactivation hook

When the runtime deactivates a grain (idle timeout, `deactivate_on_idle`, or silo shutdown), it MUST call `directory.unregister(grain_id, local_silo)`. In Phase 1 the directory entry implicitly disappeared with the activation; making this explicit is a prerequisite for the distributed directory, where the owner silo needs to know the grain is gone.

### Acceptance criteria

- [x] `IGrainDirectory` ABC defined; abstract methods listed above
- [x] `LocalGrainDirectory` implements all four methods (`register`, `lookup`, `unregister`, `resolve_or_activate`)
- [x] `register` is idempotent: second call with same `grain_id` returns the first entry
- [x] `unregister(grain_id, other_silo)` is a no-op (no cross-silo eviction)
- [x] `resolve_or_activate` returns existing entry if registered, otherwise registers + returns
- [x] Runtime calls `resolve_or_activate` before activation; all Phase 1 tests still pass
- [x] Runtime calls `unregister` during deactivation (idle timeout + shutdown)
- [x] Silo default wires `LocalGrainDirectory(local_silo)` into the runtime when running single-silo
- [x] Unit tests cover register/lookup/unregister/resolve happy paths plus idempotency

## Findings of code review

- [x] **Directory lock scope.** `LocalGrainDirectory` wraps `register`
  and `unregister` with an `asyncio.Lock` so concurrent runtime
  activations cannot race and produce two entries for the same
  `GrainId`. `resolve_or_activate` dispatches into `register`, which
  takes the same lock.
- [x] **Placement-returns-non-local safety.** In the single-silo
  setting the only candidate is the local silo. Rather than `assert`,
  the code raises `RuntimeError` with a clear message if a strategy
  violates the invariant — asserts are disabled under `python -O`.
- [x] **Runtime seam raises instead of silently failing.** When
  `entry.silo != local_silo`, `invoke()` raises
  `NotImplementedError("Remote grain invocation ... is not wired until
  task 02-16")`. That keeps dev mode deterministic and documents the
  wiring point for 02-16 reviewers.
- [x] **Unregister on deactivate.** Both the idle-collector path and
  explicit `deactivate_grain` end with
  `await self._directory.unregister(grain_id, self._local_silo)` so
  the directory state tracks the activations dict one-for-one.

## Findings of security review

- [x] **Cross-silo eviction protection.** `unregister` only removes an
  entry when the caller-supplied silo matches the recorded owner; a
  hostile peer cannot forge an unregister to evict a grain from
  another silo. Covered by `test_unregister_other_silo_is_noop`.
- [x] **Input provenance.** The directory ABC operates on in-memory
  `GrainId` / `SiloAddress` dataclasses — all callers are internal
  runtime code; there is no network-sourced input path through this
  module today. Task 02-13's distributed adapter will add the network
  edge and its own boundary validation.

## Summary of implementation

### Files created

- `src/pyleans/pyleans/cluster/directory.py` — `IGrainDirectory` ABC
  and `DirectoryEntry` value type. ABC holds the four primitives:
  `register`, `lookup`, `unregister`, `resolve_or_activate`.
- `src/pyleans/pyleans/server/local_directory.py` —
  `LocalGrainDirectory(IGrainDirectory)`: single-silo in-memory
  adapter using an `asyncio.Lock` for mutation safety and a
  monotonic `_epoch` counter.
- `src/pyleans/test/test_local_directory.py` — 14 unit tests: register
  idempotency, lookup hits/misses, unregister owner-match /
  cross-silo-noop / missing-noop, `resolve_or_activate` with both
  `PreferLocalPlacement` and `RandomPlacement`, `DirectoryEntry`
  field + frozenness.

### Files modified

- `src/pyleans/pyleans/server/runtime.py` — `GrainRuntime.__init__`
  now accepts `directory`, `local_silo`, `placement_strategy` as
  keyword-only parameters (all with sensible defaults so existing
  callers remain valid). `invoke()` calls
  `directory.resolve_or_activate(...)` before the activation lookup
  and raises `NotImplementedError` if the directory routes the call
  off-silo (dev-mode unreachable; wiring seam for 02-16).
  `deactivate_grain()` now ends with a matching
  `directory.unregister(grain_id, local_silo)` call.
- `src/pyleans/pyleans/server/silo.py` — constructs
  `LocalGrainDirectory(silo_address)` and injects it into the
  runtime alongside the existing args.
- `src/pyleans/pyleans/cluster/__init__.py` — re-exports
  `IGrainDirectory` and `DirectoryEntry`.

### Key implementation decisions

- **Constructor injection with defaults.** Keeping the runtime's
  directory optional (fallback to a local one built from the default
  silo address) means Phase 1 tests that construct a runtime directly
  stay untouched.
- **`resolve_or_activate` consults placement even in local mode.**
  This mirrors the distributed path exactly, so task 02-13's
  implementation doesn't have to change the call shape — only the
  directory's internal mechanics.
- **Atomic `register` under a lock.** Two concurrent activations for
  the same grain must receive the same entry; without the lock the
  `dict.get`/`dict.__setitem__` pair could interleave and produce two
  entries. The lock is per-directory and short-held.
- **`unregister` is authority-checked.** Only the owner-silo can
  evict its own entry. This is the semantics the distributed
  directory will need to preserve under network eviction races.

### Deviations from the original design

- The task's `resolve_or_activate` sketch used a standalone
  `assert chosen == self._local`. Replaced with a `RuntimeError`
  raise so the invariant survives `python -O`.

### Test coverage

- 14 new directory tests (`test_local_directory.py`).
- Full suite: 732 passing (up from 718 at task start; Phase 1
  semantics unchanged).
- `ruff check` clean; `ruff format` clean; `pylint` 10.00/10;
  `mypy src/pyleans/pyleans` clean (no new errors anywhere).
