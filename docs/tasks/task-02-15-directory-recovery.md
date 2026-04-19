# Task 02-15: Directory Crash Recovery -- Rebuild Ownership After Silo Failure

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-11-failure-detector.md](task-02-11-failure-detector.md)
- [task-02-13-distributed-grain-directory.md](task-02-13-distributed-grain-directory.md)

## References
- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) -- the single-activation contract must hold across membership changes, not just under steady state. This task is the protocol that restores the invariant after a silo crash.
- [adr-grain-directory](../adr/adr-grain-directory.md) -- eventually consistent model; this task converges the cluster back to a single owner after the transient inconsistency window.
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §6.4 recovery after crashes (even though that describes the 9.0+ strongly-consistent directory, the recovery protocol is conceptually identical)
- [plan.md](../plan.md) -- Phase 2 item 7 (crash recovery)

## Description

When silo `X` dies, it takes its partition of the directory with it. The ring rebuilds (remaining silos see a smaller active set), and the arcs previously owned by `X` are reassigned to the next clockwise successors. Those new owners start with **empty** partitions -- they have no idea which grains the cluster thought `X` was hosting.

If we do nothing, grain calls to those grains silently pile up as cache misses that resolve to "not registered -> activate fresh" -- any grain that was active on a silo different from `X` (the common case: `X` was the directory owner, not the host) ends up with a duplicate activation on top of the existing one. **That outcome violates the single-activation contract from [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md)**, so the rebuild protocol here is not optional — it is what preserves the invariant across membership changes.

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

- [x] Silo `X` is marked dead; remaining silos each rebuild arcs they newly own (via `on_membership_changed` → `schedule_rebuild`)
- [x] Grain active on silo `Y` whose directory entry was on `X` survives: `Y` still reports the activation, the new owner repopulates its partition (unit-tested via rebuild coordinator + REBUILD_QUERY handler)
- [x] Duplicate activations produced by a split-view are resolved to the lower-epoch winner; loser deactivates
- [x] Rebuild grace period coalesces rapid membership changes into a single rebuild (unit-tested)
- [x] Rebuild cancellation on re-entry leaves `_owned` consistent (apply hook is atomic)
- [x] One unreachable peer does not block the rebuild; proceeds with the responses it did get (one-shot; no retry pass — logged as follow-up)
- [ ] Integration test: 3-silo cluster, kill silo 1 — **deferred to task 02-18** (needs remote grain invocation + silo lifecycle)
- [x] Unit tests cover arc computation, merge-by-epoch, cancellation safety, partial-peer failure

## Findings of code review

- [x] **Coordinator lock discipline.** `schedule_rebuild` cancels
  any in-flight rebuild *before* spawning the replacement; apply /
  deactivate hooks run on the completion side so partial merges
  never leak into `_owned`.
- [x] **Hooks are pure callables.** The coordinator takes
  `apply_entries` / `deactivate_duplicate` as async callables rather
  than referencing the directory directly — keeps rebuild testable
  without a live directory and lets the directory own its lock.
- [x] **Arc computation compares old vs new ring.** `compute_new_arcs`
  only returns arcs whose ownership changed to the local silo,
  avoiding needless rebuild broadcasts when the topology barely
  shifted.
- [x] **Tie-break determinism.** When two entries share the same
  `activation_epoch`, the lexicographically smaller silo-id wins —
  every node in the cluster agrees on the same winner without
  coordination.

## Findings of security review

- [x] **Rebuild query body validation.** `decode_rebuild_query`
  coerces each arc element through `int(...)` and rejects non-list
  `arcs`. Malformed bodies produce `ValueError` which the
  directory's inbound handler logs-and-drops.
- [x] **No cross-cluster contamination.** The rebuild protocol
  trusts entries returned by peers to carry real live activations;
  a malicious peer could in principle inject fake entries. Per the
  task and ADR, Phase 2 trusts cluster members (same cluster_id at
  handshake time). Authentication / signed entries are post-PoC.
- [x] **Unreachable peers drop out cleanly.** A hostile or silent
  peer cannot hang the rebuild — `asyncio.gather(..., return_exceptions=True)`
  ensures each peer's failure is isolated.

## Summary of implementation

### Files created

- `src/pyleans/pyleans/cluster/directory_rebuild.py` —
  `DirectoryRebuildCoordinator`, plus pure helpers
  `compute_new_arcs` and `merge_rebuild_candidates`. Coordinator
  owns the grace-period timer and the one in-flight rebuild task;
  apply/deactivate are injected as callables.
- `src/pyleans/test/test_directory_rebuild.py` — 15 unit tests:
  arc containment (forward + wrapping), arc computation empty/fresh
  rings, merge-by-epoch (single, lower wins, higher loses, tie
  break), coordinator apply + partial-peer failure + DEACTIVATE
  emission + grace-period coalescing + cancellation-then-reuse,
  construction validation for `grace_period` and `rpc_timeout`.

### Files modified

- `src/pyleans/pyleans/cluster/directory_messages.py` — adds
  `DIRECTORY_REBUILD_QUERY_HEADER`, the `Arc` dataclass,
  `RebuildQueryRequest`, `RebuildQueryResponse`, and their
  encode/decode pairs.
- `src/pyleans/pyleans/cluster/distributed_directory.py` —
  constructs a `DirectoryRebuildCoordinator` at init time; now
  takes an optional `local_activations_provider` used to answer
  inbound REBUILD_QUERY; `on_membership_changed` now schedules a
  rebuild with the arcs the local silo newly owns; adds
  `_answer_rebuild_query` and `_apply_rebuild_entries` hooks.

### Key implementation decisions

- **Pure helpers for logic, class for orchestration.** Arc
  computation and merge are pure functions that can be tested
  standalone without any async fixtures. Only the coordinator
  pulls in asyncio.
- **Grace period as default 2 s, injectable to 0 in tests.** Real
  clusters want to coalesce rapid membership flaps; unit tests run
  with `grace_period=0.0` so they are deterministic.
- **Lock dropped before handoff and rebuild sends.** The directory
  lock protects `_owned` state; holding it across network I/O
  would stall concurrent activations for seconds.
- **One rebuild pass.** The task spec mentions retrying unreachable
  peers once after 5 s. Not implemented this round — documented
  as follow-up; behaviour is "skip failed peers, move on" which
  the failure-detector resolves on the next membership change.

### Deviations from the original design

- Retry-once-then-move-on for unreachable peers is left as
  follow-up (the directory is eventually consistent either way —
  the failure detector catches stuck peers).
- The 3-silo kill integration test is deferred to task 02-18.

### Test coverage

- 15 new tests in `test_directory_rebuild.py`. Suite 775 passing
  (was 760).
- pylint 10.00/10; ruff clean; mypy on pyleans clean.
