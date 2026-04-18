# Task 02-12: Grain Directory Port -- `IGrainDirectory` ABC and Local Adapter

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-08-grain-runtime.md](task-01-08-grain-runtime.md)

## References
- [adr-grain-directory](../adr/adr-grain-directory.md)
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §6.6 pluggable directory

## Description

Before building the distributed grain directory, extract the existing in-memory directory behavior (currently a `dict[GrainId, GrainActivation]` inside `GrainRuntime`) behind an abstract `IGrainDirectory` port. This is a refactor, not a feature addition: Phase 1 behavior must remain identical after this task.

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

- [ ] `IGrainDirectory` ABC defined; abstract methods listed above
- [ ] `LocalGrainDirectory` implements all five methods
- [ ] `register` is idempotent: second call with same `grain_id` returns the first entry
- [ ] `unregister(grain_id, other_silo)` is a no-op (no cross-silo eviction)
- [ ] `resolve_or_activate` returns existing entry if registered, otherwise registers + returns
- [ ] Runtime calls `resolve_or_activate` before activation; all Phase 1 tests still pass
- [ ] Runtime calls `unregister` during deactivation (idle timeout + shutdown)
- [ ] Silo default wires `LocalGrainDirectory(local_silo)` into the runtime when running single-silo
- [ ] Unit tests cover register/lookup/unregister/resolve happy paths plus idempotency

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
