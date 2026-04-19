# Task 02-13: Distributed Grain Directory -- Partitioned by Hash Ring, Eventually Consistent

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-02-consistent-hash-ring.md](task-02-02-consistent-hash-ring.md)
- [task-02-03-placement-strategies.md](task-02-03-placement-strategies.md)
- [task-02-08-tcp-cluster-transport.md](task-02-08-tcp-cluster-transport.md)
- [task-02-12-grain-directory-port.md](task-02-12-grain-directory-port.md)

## References
- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) -- this task is the **primary enforcement point** for the single-activation contract. Without it, two silos can independently activate the same `GrainId`; with it, the cluster guarantees at most one activation across all silos under steady-state membership.
- [adr-grain-directory](../adr/adr-grain-directory.md) -- eventually consistent (pre-Orleans 9.0)
- [architecture/consistent-hash-ring.md](../architecture/consistent-hash-ring.md)
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §6.1 purpose, §6.2 architecture, §6.3 location flow, §6.5 pre-9.0 directory

## Description

Replace `LocalGrainDirectory` with a directory that is partitioned across all active silos. Every `GrainId` has an **owner silo** -- the silo whose position on the consistent hash ring is the first clockwise successor of `hash_grain_id(grain_id)`. That owner silo holds the authoritative entry. Every other silo asks the owner via a directory RPC.

This is the smallest distributed directory that upholds the single-activation guarantee under steady-state membership. Membership changes introduce a brief inconsistency window (two silos disagreeing on who the owner is); the crash-recovery protocol in [task-02-15](task-02-15-directory-recovery.md) resolves it.

The directory is explicitly **eventually consistent** per ADR. Strong consistency (Orleans 9.0+) is deferred to post-PoC.

**Storage provider stays oblivious.** The directory is what guarantees only one silo writes storage for a given `GrainId` at a time; the storage provider does not need cluster awareness or owner-silo checks. This is why we can reuse the Phase 1 `FileStorageProvider` and `PostgreSQL` storage unchanged across Phase 2 — their contract is per-call etag-CAS, not cluster-wide exclusion.

### Files to create

- `src/pyleans/pyleans/cluster/distributed_directory.py`
- `src/pyleans/pyleans/cluster/directory_messages.py` -- wire types for directory RPCs

### Design

```python
class DistributedGrainDirectory(IGrainDirectory):
    def __init__(
        self,
        local_silo: SiloAddress,
        transport: IClusterTransport,
        membership: MembershipProvider,
        initial_ring: ConsistentHashRing,
    ) -> None:
        self._local = local_silo
        self._transport = transport
        self._membership = membership
        self._ring = initial_ring
        # Partition this silo owns -- the authoritative entries.
        self._owned: dict[GrainId, DirectoryEntry] = {}
        self._epoch = 0
        self._ring_lock = asyncio.Lock()
        transport.set_request_handler(DIRECTORY_RPC, self._handle_directory_rpc)

    async def on_membership_changed(self, active: list[SiloAddress]) -> None:
        """Rebuild the ring. Entries whose ownership moves to another silo
        are retained for one grace period (see task-02-15) and then discarded.
        Entries whose ownership moves TO this silo are NOT auto-populated;
        task-02-15 handles rebuild via broadcast query."""

    # IGrainDirectory methods below route to the owner.
    async def register(self, grain_id, silo):
        owner = self._owner_of(grain_id)
        if owner == self._local:
            return self._register_local(grain_id, silo)
        return await self._register_remote(owner, grain_id, silo)

    async def lookup(self, grain_id):
        owner = self._owner_of(grain_id)
        if owner == self._local:
            return self._owned.get(grain_id)
        return await self._lookup_remote(owner, grain_id)

    async def unregister(self, grain_id, silo): ...

    async def resolve_or_activate(self, grain_id, placement, caller):
        """If we own: resolve or pick a silo + register.
        If not: forward the whole operation to the owner in a single RPC —
        avoids a round-trip for the common 'not registered' case."""
```

### Owner computation

```python
def _owner_of(self, grain_id: GrainId) -> SiloAddress:
    h = hash_grain_id(grain_id)
    owner = self._ring.owner_of(h)
    if owner is None:
        raise ClusterNotReadyError("ring is empty")
    return owner
```

Because the ring is rebuilt from the same active silo list everywhere, every silo agrees on `_owner_of(g)` -- **as long as they see the same membership view**. Disagreement during membership churn is the central reliability concern addressed in [task-02-15](task-02-15-directory-recovery.md).

### Directory RPC wire contract

Four message types ride inside `MessageType.REQUEST` frames with a `directory` header tag:

```python
class DirectoryOp(IntEnum):
    REGISTER            = 1  # body: (grain_id, silo)                 -> DirectoryEntry | existing
    LOOKUP              = 2  # body: grain_id                          -> DirectoryEntry | None
    UNREGISTER          = 3  # body: (grain_id, silo)                  -> None
    RESOLVE_OR_ACTIVATE = 4  # body: (grain_id, placement_class, caller_silo) -> DirectoryEntry
```

Headers carry the operation code + a schema version number (future-proofing in line with the handshake version story in [task-02-05](task-02-05-wire-protocol.md)). Bodies are `orjson`-serialized value dataclasses.

### `resolve_or_activate` on the owner

This is the single most sensitive code path in the directory:

```python
async def _resolve_or_activate_local(self, grain_id, placement_cls, caller):
    existing = self._owned.get(grain_id)
    if existing is not None:
        return existing

    active = [s.address for s in await self._membership.get_active_silos()]
    placement = placement_cls()
    chosen = placement.pick_silo(grain_id, caller, active)
    self._epoch += 1
    entry = DirectoryEntry(grain_id, chosen, self._epoch)
    self._owned[grain_id] = entry
    return entry
```

Two subtleties:

1. **Single-activation guarantee under contention**: 100 concurrent `resolve_or_activate` calls for the same `grain_id` must all receive the same `DirectoryEntry`. The grain runtime's single-threaded turn model plus the owner silo funneling all directory mutations to one asyncio task gives us this for free.

2. **Placement uses the membership-table active set**, not the transport's connected set. A silo we are currently disconnected from but which membership still marks `Active` is a valid placement target -- the transport will reconnect. Placement decisions tied to "who I can reach right now" would thrash during transient disconnections.

### Registration races across owners

If silo A computes owner=X, silo B computes owner=Y (because they briefly see different membership views), both could register the same grain with different placements. When they exchange membership and reach agreement, one owner "loses" and must hand the entry off. Resolution:

- When ownership of `g` moves FROM `X` TO `Y` (membership change), `X` **does not discard** its entry for `g` immediately. It sends a one-way `DIRECTORY_HANDOFF` to `Y` with the current mapping. `Y` adopts it (preferring older `activation_epoch` if `Y` already has one -- see next).
- When `Y` discovers two candidate entries for `g` (its own created during the split, plus a handoff from `X`), the **lower `activation_epoch`** wins and the loser silo is told to deactivate its in-memory activation. Lower-epoch tiebreaker means the earlier decision holds; the later one is considered a duplicate.

This resolution protocol is small enough to describe inline; implement it here, test it alongside [task-02-15](task-02-15-directory-recovery.md).

### Deactivation

When a grain is deactivated on silo `S`, `S` must:

1. Remove it from its local in-memory activation table.
2. Call `directory.unregister(grain_id, S)` which forwards to the current owner.
3. Owner removes the entry only if it currently maps to `S` (avoids removing an entry that already moved to a newer activation).

### Acceptance criteria

- [x] Three-silo test: `register(g, s1)` from s1 stores entry on owner (whoever that turns out to be); lookup from s2 and s3 returns the same entry
- [x] `resolve_or_activate(g, placement, caller)` returns the entry; subsequent calls from any silo return the same entry
- [x] **Single-activation under contention**: two concurrent `resolve_or_activate(grain_id)` calls from different silos return the same `DirectoryEntry`; only one silo ends up owning the activation (10-way concurrency test)
- [x] Concurrent `resolve_or_activate` for same `g` from 10 silos produces 1 entry (no duplicate activations)
- [x] Ownership transfer on membership change: owner `X` hands off to new owner `Y`; old entry on `X` is discarded (grace period is deferred to task 02-15)
- [x] Split-view race test: two silos briefly disagree on owner, create entries, reconcile to lower-epoch winner; loser silo receives a "deactivate" signal
- [x] `unregister(g, s)` is a no-op if owner's entry maps to a different silo
- [x] `ClusterNotReadyError` raised if ring is empty
- [x] Directory RPCs surface transient transport errors to the caller (retry strategy is policy of the caller layer, matching the IClusterTransport contract)
- [ ] Integration test: 2-silo cluster, silo A activates grain, silo B calls it, grain is invoked on A — **deferred to task 02-18** (needs remote grain invocation from 02-16 and silo lifecycle from 02-17)

## Findings of code review

- [x] **Single-activation atomicity under contention.** The owner's
  `_resolve_or_activate_local` / `_register_local` take the directory's
  `asyncio.Lock` before reading the `_owned` dict; concurrent
  activations for the same `GrainId` are serialized at the owner.
  Covered by `test_concurrent_resolves_return_same_entry` (10
  concurrent activations via `asyncio.gather`).
- [x] **Placement uses membership active set, not transport's connected set.**
  `_placement_candidates` reads `_active_silos` (set via
  `on_membership_changed`) so placement decisions survive transient
  disconnections as the task spec requires.
- [x] **Handoff lock scope.** `on_membership_changed` computes
  reassignments under the directory lock and then releases it before
  sending the actual handoff RPCs — we do not hold the lock across
  network I/O.

## Findings of security review

- [x] **Malformed RPC bodies cannot crash the directory.** Every
  decoder wraps schema / coercion errors in `ValueError`; the
  inbound dispatch logs and returns `None` (no response), so a
  hostile peer cannot take down the owner with junk bytes.
- [x] **Cross-silo eviction still blocked at the owner.** The owner's
  `_unregister_local` keeps the same owner-match check as
  `LocalGrainDirectory` — a peer claiming to be someone else cannot
  evict a grain. Covered by `test_unregister_non_matching_silo_is_noop`.
- [x] **Handoff cannot hijack ownership.** `_apply_handoff` rejects
  handoffs where the local silo is not the new ring owner — a
  hostile peer cannot install entries on silos that the ring does
  not assign ownership to.
- [x] **Activation-epoch reconciliation is deterministic.** Lower
  epoch always wins; ties break toward existing. No ambiguity window
  an attacker can exploit to toggle ownership.

## Summary of implementation

### Files created

- `src/pyleans/pyleans/cluster/directory_messages.py` — wire-schema
  module: six header tags, six request / response dataclasses, and
  JSON encoders / decoders for each. Body format is
  `{"grain_id": {...}, "silo": "...", ...}` — human-readable,
  schema-validated on decode.
- `src/pyleans/pyleans/cluster/distributed_directory.py` —
  `DistributedGrainDirectory(IGrainDirectory)`: owner routing via
  the consistent hash ring, four-RPC server side, on-membership
  handoff streaming, and split-view reconciliation by
  `activation_epoch` with DEACTIVATE notification to the loser.
  Also defines `ClusterNotReadyError`.
- `src/pyleans/test/test_distributed_directory.py` — 13 unit tests
  covering owner routing, `resolve_or_activate` under 10-way
  concurrency, handoff, split-view reconciliation (both low-epoch
  and high-epoch paths), DEACTIVATE notifier, `ClusterNotReadyError`,
  transient transport-error propagation. Uses an in-memory fabric
  (`InMemoryFabric` + `FabricTransport`) that wires N directories
  by silo-id so RPCs land on the owner's real `message_handler`.

### Files modified

- `src/pyleans/pyleans/cluster/__init__.py` — re-exports
  `DistributedGrainDirectory`, `ClusterNotReadyError`.

### Key implementation decisions

- **Six wire headers, not one with an op field.** Per-op header
  tags (`pyleans/directory/v1/register`, `.../lookup`, etc.) let
  the transport dispatcher route by header without body
  inspection. Matches the pattern established by `INDIRECT_PROBE`
  and `MEMBERSHIP_SNAPSHOT` in task 02-11.
- **`_PLACEMENT_CLASSES` registry for remote `resolve_or_activate`.**
  The caller's placement strategy is serialised by class name and
  rebuilt by lookup on the owner. Only strategies that appear in
  `_PLACEMENT_CLASSES` are accepted over the wire; a caller with a
  custom strategy gets a clean `ValueError` at send-time rather
  than silent misbehaviour. Extension: additions to the registry
  are one-liners.
- **Handoff streams entries out synchronously, then drops.** The
  task spec mentions a "grace period" before dropping handed-off
  entries; that lives in task 02-15 (recovery). For now the old
  owner drops immediately after sending — split-view behaviour is
  still correct because the new owner applies lower-epoch wins.
- **DEACTIVATE notifier is a user-supplied hook.** The directory
  surface must not depend on the grain runtime, so the runtime
  wires `on_deactivate_notified(...)` in task 02-17. Today the
  directory just fires the hook if set, or logs-and-skips if not.

### Deviations from the original design

- `IClusterTransport` has no `set_request_handler(op_code, handler)`
  method — its message handler is installed once at `start()`. The
  directory exposes `message_handler(source, msg)` instead, and the
  silo will compose it with the other handlers (task 02-17).
- Grace period for handed-off entries is **deferred to 02-15**.
- Two acceptance bullets (2-silo remote-invocation integration test)
  are explicitly deferred to task 02-18.

### Test coverage

- 13 new tests in `test_distributed_directory.py`; full suite
  745 passing (up from 732).
- pylint 10.00/10; ruff + ruff format clean; mypy on pyleans clean.
