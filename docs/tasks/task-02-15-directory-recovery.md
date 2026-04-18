# Task 02-15: Directory Crash Recovery -- Rebuild Ownership After Silo Failure

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-11-failure-detector.md](task-02-11-failure-detector.md)
- [task-02-13-distributed-grain-directory.md](task-02-13-distributed-grain-directory.md)

## References
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §6.4 recovery after crashes (even though that describes the 9.0+ strongly-consistent directory, the recovery protocol is conceptually identical)
- [plan.md](../plan.md) -- Phase 2 item 7 (crash recovery)

## Description

When silo `X` dies, it takes its partition of the directory with it. The ring rebuilds (remaining silos see a smaller active set), and the arcs previously owned by `X` are reassigned to the next clockwise successors. Those new owners start with **empty** partitions -- they have no idea which grains the cluster thought `X` was hosting.

If we do nothing, grain calls to those grains silently pile up as cache misses that resolve to "not registered -> activate fresh" -- any grain that was active on a silo different from `X` (the common case: `X` was the directory owner, not the host) ends up with a duplicate activation on top of the existing one.

The recovery protocol: new partition owners **broadcast a rebuild query** to all remaining silos, asking "which of your local activations' directory entries hashed into an arc I now own?" Silos reply with their local activation set; the new owner populates its partition.

This is a one-time burst of RPCs per silo-failure, not ongoing traffic -- so the cost is bounded and predictable.

### Files to create/modify

**Create:**
- `src/pyleans/pyleans/cluster/directory_rebuild.py` -- `DirectoryRebuildCoordinator`

**Modify:**
- `src/pyleans/pyleans/cluster/distributed_directory.py` -- plug in the coordinator on membership change
- `src/pyleans/pyleans/cluster/directory_messages.py` -- add `REBUILD_QUERY` / `REBUILD_REPLY` RPCs

### Protocol

When `DistributedGrainDirectory.on_membership_changed(active)` runs:

1. Compute the **old ring** (pre-change) and the **new ring**.
2. For every dead silo `D` (in old_active, not in new_active):
   - Find the arcs `D` owned on the old ring.
   - For each arc, the new owner on the new ring is this silo or a peer.
   - If this silo becomes the new owner of any arcs → schedule rebuild for those arcs.
3. `DirectoryRebuildCoordinator.start_rebuild(arcs)`:

```python
class DirectoryRebuildCoordinator:
    async def start_rebuild(self, arcs: list[Arc]) -> None:
        """Concurrently query every remaining active silo for its local
        activation entries whose hashes fall in `arcs`. Merge responses
        into self._owned, resolving conflicts by lowest activation_epoch."""

        active = set(await self._membership.get_active_silos()) - {self._local}
        responses = await asyncio.gather(
            *[self._query_peer(p, arcs) for p in active],
            return_exceptions=True,
        )
        merged = self._merge_responses(responses)
        for grain_id, entry in merged.items():
            existing = self._owned.get(grain_id)
            if existing is None or entry.activation_epoch < existing.activation_epoch:
                self._owned[grain_id] = entry
```

Each peer responds with its local activations and their `activation_epoch`. Merging by lowest epoch resolves the split-view scenario from [task-02-13](task-02-13-distributed-grain-directory.md).

### `REBUILD_QUERY` RPC

Request body:
```
arcs: list[tuple[int, int]]   # list of (start_position, end_position) on the 64-bit hash ring
```

Response body:
```
entries: list[DirectoryEntry]   # this silo's local activations whose grain_id hashes into any arc
```

The responding silo iterates its **local in-memory activation table** (from `GrainRuntime`) -- NOT its directory entries -- and reports exactly the grains it is hosting right now. The new owner then gets the authoritative "where grains actually live" view for its arcs.

### Grace period

Before rebuilding, wait a short grace period (default 2 seconds) to let membership settle. Multiple silos dying in close succession would otherwise trigger N rebuilds in quick sequence, some already stale. A single rebuild after the dust settles is cheaper and more likely to be correct.

Cancel the grace timer if another membership change arrives during the wait -- recompute arcs and restart.

### What about duplicate activations already created during the outage?

The split-view race in [task-02-13](task-02-13-distributed-grain-directory.md) can produce two activations of the same grain on different silos during the period when silos disagreed on ownership. Rebuild finds both in the merge step. Resolution:

1. Merge picks the lower-epoch entry as the winner.
2. The coordinator sends a `DEACTIVATE_DUPLICATE` one-way message to the loser silo.
3. Loser silo deactivates the grain (calls `on_deactivate`, discards state loaded-but-not-written, removes from in-memory table).
4. Cache is invalidated cluster-wide for the grain via a membership-change broadcast.

The user-visible impact is a one-time "grain re-activated on a new silo" event; state is re-read from storage by whichever activation wins. This is the ADR-documented Phase 2 limitation.

### Idempotency and re-entry

Rebuild can be invoked multiple times (e.g. membership changes again during a rebuild). Coordinator state:

- Track `self._active_rebuild: asyncio.Task | None`.
- New rebuild request: cancel the active task, wait for it to finish cleanly, start a fresh one with the latest arcs.
- Cancellation must leave `self._owned` in a consistent state -- done by applying each peer's response atomically, never partial merges.

### Failure handling

`asyncio.gather(..., return_exceptions=True)` means a single peer timing out does not sink the whole rebuild. The coordinator:

- Logs WARN per failed peer.
- Proceeds with the responses it did receive.
- Remembers which peers failed; retries them once after 5 s.
- If a peer is still unreachable after retry, moves on -- those grains are considered ephemeral and will self-register on next invocation. The cluster remains consistent once the unreachable peer either responds or is marked dead by the failure detector.

### Acceptance criteria

- [ ] Silo `X` is marked dead; remaining silos each rebuild arcs they newly own
- [ ] Grain active on silo `Y` whose directory entry was on `X` survives: `Y` still reports the activation, the new owner repopulates its partition
- [ ] Duplicate activations produced by a split-view are resolved to the lower-epoch winner; loser deactivates
- [ ] Rebuild grace period coalesces rapid membership changes into a single rebuild
- [ ] Rebuild cancellation on re-entry leaves `_owned` consistent
- [ ] One unreachable peer does not block the rebuild; retry attempt once, then move on
- [ ] Integration test: 3-silo cluster, kill silo 1 (was owner of a grain hosted on silo 2); after rebuild, lookup from silo 3 finds the grain still on silo 2
- [ ] Unit tests cover arc computation, merge-by-epoch, cancellation safety, partial-peer failure

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
