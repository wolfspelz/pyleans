# Task 31: Distributed Grain Directory -- Partitioned by Hash Ring, Eventually Consistent

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-20-consistent-hash-ring.md](task-20-consistent-hash-ring.md)
- [task-21-placement-strategies.md](task-21-placement-strategies.md)
- [task-26-tcp-cluster-transport.md](task-26-tcp-cluster-transport.md)
- [task-30-grain-directory-port.md](task-30-grain-directory-port.md)

## References
- [adr-grain-directory](../adr/adr-grain-directory.md) -- eventually consistent (pre-Orleans 9.0)
- [architecture/consistent-hash-ring.md](../architecture/consistent-hash-ring.md)
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §6.1 purpose, §6.2 architecture, §6.3 location flow, §6.5 pre-9.0 directory

## Description

Replace `LocalGrainDirectory` with a directory that is partitioned across all active silos. Every `GrainId` has an **owner silo** -- the silo whose position on the consistent hash ring is the first clockwise successor of `hash_grain_id(grain_id)`. That owner silo holds the authoritative entry. Every other silo asks the owner via a directory RPC.

This is the smallest distributed directory that upholds the single-activation guarantee under steady-state membership. Membership changes introduce a brief inconsistency window (two silos disagreeing on who the owner is); the crash-recovery protocol in [task-33](task-33-directory-recovery.md) resolves it.

The directory is explicitly **eventually consistent** per ADR. Strong consistency (Orleans 9.0+) is deferred to post-PoC.

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
        are retained for one grace period (see task-33) and then discarded.
        Entries whose ownership moves TO this silo are NOT auto-populated;
        task-33 handles rebuild via broadcast query."""

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

Because the ring is rebuilt from the same active silo list everywhere, every silo agrees on `_owner_of(g)` -- **as long as they see the same membership view**. Disagreement during membership churn is the central reliability concern addressed in [task-33](task-33-directory-recovery.md).

### Directory RPC wire contract

Four message types ride inside `MessageType.REQUEST` frames with a `directory` header tag:

```python
class DirectoryOp(IntEnum):
    REGISTER            = 1  # body: (grain_id, silo)                 -> DirectoryEntry | existing
    LOOKUP              = 2  # body: grain_id                          -> DirectoryEntry | None
    UNREGISTER          = 3  # body: (grain_id, silo)                  -> None
    RESOLVE_OR_ACTIVATE = 4  # body: (grain_id, placement_class, caller_silo) -> DirectoryEntry
```

Headers carry the operation code + a schema version number (future-proofing in line with the handshake version story in [task-23](task-23-wire-protocol.md)). Bodies are `orjson`-serialized value dataclasses.

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

This resolution protocol is small enough to describe inline; implement it here, test it alongside [task-33](task-33-directory-recovery.md).

### Deactivation

When a grain is deactivated on silo `S`, `S` must:

1. Remove it from its local in-memory activation table.
2. Call `directory.unregister(grain_id, S)` which forwards to the current owner.
3. Owner removes the entry only if it currently maps to `S` (avoids removing an entry that already moved to a newer activation).

### Acceptance criteria

- [ ] Three-silo test: `register(g, s1)` from s1 stores entry on owner (whoever that turns out to be); lookup from s2 and s3 returns the same entry
- [ ] `resolve_or_activate(g, placement, caller)` returns the entry; subsequent calls from any silo return the same entry
- [ ] Concurrent `resolve_or_activate` for same `g` from 10 silos produces 1 entry (no duplicate activations)
- [ ] Ownership transfer on membership change: owner `X` hands off to new owner `Y`; old entry on `X` is discarded after grace period
- [ ] Split-view race test: two silos briefly disagree on owner, create entries, reconcile to lower-epoch winner; loser silo receives a "deactivate" signal
- [ ] `unregister(g, s)` is a no-op if owner's entry maps to a different silo
- [ ] `ClusterNotReadyError` raised if ring is empty
- [ ] Directory RPCs survive transient transport errors (retry at the caller; no permanent loss)
- [ ] Integration test: 2-silo cluster, silo A activates grain, silo B calls it, grain is invoked on A

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
