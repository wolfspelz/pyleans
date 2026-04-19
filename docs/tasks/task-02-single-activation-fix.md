# Task 02-single-activation-fix: retroactive upgrade to the single-activation cluster contract

> **Status: temporary.** This task is a retroactive remediation plan. Its purpose is to bring the repository — code **and** documentation — from the current non-ideal state (where two silos would each host their own activation of the same `GrainId`, violating the virtual-actor contract) to the ideal state described in [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md). When all steps in this file have been executed, the plan's work is done and this file is archived (keep the file for history; update cross-references in the individual Phase 2 task Summaries).
>
> **Coding rules**: Follow [CLAUDE.md](../../CLAUDE.md), [.claude/coding-rules.md](../../.claude/coding-rules.md), and [.claude/testing-rules.md](../../.claude/testing-rules.md) strictly.

## Problem statement

Today — after Phase 1 is complete — running two silos on localhost would produce two independent activations of `Counter("a")`, each with its own state racing on the shared storage backend. This is exactly the failure mode the virtual-actor model forbids. The cluster subsystem needed to enforce single activation (hash ring + distributed directory + cluster transport + failure detection + directory cache/recovery + lifecycle stages) does not yet exist.

[adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) declares the contract that makes this right: single activation is a **cluster responsibility**, enforced at the intersection of membership, `IGrainDirectory`, and `IClusterTransport`. The runtime consults the directory inside `GrainRuntime.invoke()` and forwards remote calls over cluster transport, so external clients see silo-transparent behaviour through any gateway.

This task is the single plan that closes the gap — in both directions:

- **Docs**: every existing ADR and task spec in the repo is audited against the single-activation contract, and any that were written before that contract was articulated (or that take shortcuts inconsistent with it) are rewritten. The Phase 2 task list is made "ideal" — meaning executing it produces a codebase that fully implements [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md).
- **Code**: the ideal task list, once agreed, is executed end-to-end.

## Dependencies

- All of Phase 1 (tasks 01-01 through 01-21) — complete.
- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) — the contract this task implements.

## Supersedes / superseded by

- Supersedes: none.
- Superseded by: tasks 02-01 through 02-21 collectively (as they stand after Phase A of this plan). When all are `[x]`, archive this file.

## References

- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) — primary contract.
- [adr-grain-directory](../adr/adr-grain-directory.md) — consistency model and hash-ring choice.
- [adr-cluster-transport](../adr/adr-cluster-transport.md) — pluggable cluster mesh.
- [adr-cluster-access-boundary](../adr/adr-cluster-access-boundary.md) — gateway is sole external entry.
- [adr-provider-interfaces](../adr/adr-provider-interfaces.md) — hexagonal ports (including membership).
- [adr-network-port-for-testability](../adr/adr-network-port-for-testability.md) — `INetwork` foundation.
- [adr-dev-mode](../adr/adr-dev-mode.md) — Phase 1 single-silo scope rationale.
- [adr-concurrency-model](../adr/adr-concurrency-model.md) — turn-based single-activation at the grain level.
- [plan.md §Phase 2](../plan.md) — cluster scope.
- [docs/adr/README.md](../adr/README.md) — ADR index and template.
- [docs/tasks/README.md](README.md) — task index and dependency graph.

## Plan

This task has three phases. Phase A completes before any Phase 2 code work starts; Phases B and C then proceed task-by-task.

---

### Phase A — Documentation alignment (audit + rewrite)

Goal: every ADR and every task spec in the repo is internally consistent with [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md). The Phase 2 task list that emerges is the "ideal" list — executing it produces ADR-compliant code with no residual gaps.

#### A.1 — ADR audit

For every file in [docs/adr/](../adr/), verify:

- Cross-references to single-activation are present where appropriate. At minimum: [adr-grain-directory](../adr/adr-grain-directory.md), [adr-cluster-transport](../adr/adr-cluster-transport.md), [adr-cluster-access-boundary](../adr/adr-cluster-access-boundary.md), and [adr-provider-interfaces](../adr/adr-provider-interfaces.md) each link to `adr-single-activation-cluster` in their "Related" section.
- No contradiction with the contract. Specifically: no ADR describes a design in which per-silo activation is acceptable, or in which clients carry routing logic, or in which a side-channel file substitutes for the distributed directory.
- `adr-grain-directory` explicitly states that the directory is the enforcement point for single activation (it currently focuses on the hash-ring/consistency choice; extend its Consequences section to name the single-activation property it delivers).
- `adr-dev-mode` notes explicitly that the single-silo simplification is a **scope** decision, not a **correctness** exemption — the single-activation contract is still in force, it's just trivially satisfied when there's only one silo.

**Deliverable:** a short commit updating any ADR that needs a Related-link, a clarification, or a contradiction removed. ADR content is not rewritten wholesale — edits are minimal and targeted.

#### A.2 — Phase 1 task spec audit

For every file `task-01-*.md`, verify:

- The design sketch does not preclude Phase 2's cluster hooks. In particular:
  - `task-01-08-grain-runtime.md`: the `GrainRuntime` constructor signature either already accepts `directory: IGrainDirectory | None` and `cluster_transport: IClusterTransport | None`, or its spec calls out that Phase 2 will add these parameters (with the single hook site in `invoke()`).
  - `task-01-17-silo.md`: the `Silo` constructor signature likewise leaves room for the Phase 2 cluster subsystem, or explicitly scopes it out with a forward reference.
  - `task-01-08` and `task-01-17` summaries note that Phase 2 will add the routing hook without changing Phase 1's semantics.
- Acceptance criteria reference the single-activation contract as a future constraint (even if the Phase 1 task trivially satisfies it with a single silo).

**Deliverable:** targeted edits to `task-01-08` and `task-01-17` specs (and their Summary sections if needed) to mention the Phase 2 extension points. Do not rewrite acceptance criteria that have already been met.

#### A.3 — Phase 2 task spec audit

For every file `task-02-*.md`, verify:

- The task's "Description" section references [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) where relevant. At minimum 02-12, 02-13, 02-14, 02-15, 02-16, 02-17, 02-18 must cite it, since they are the tasks that materially deliver the contract.
- Design sketches follow [.claude/coding-rules.md](../../.claude/coding-rules.md) (type hints, `...` bodies, consistent naming).
- Acceptance criteria state single-activation explicitly where the task is a load-bearing enforcement point:
  - 02-13 acceptance: "Two concurrent `resolve_or_activate(grain_id)` calls from different silos return the same `DirectoryEntry`; only one silo ends up owning the activation."
  - 02-16 acceptance: "A client call through any silo's gateway for a remote grain is forwarded transparently; activation count for that `GrainId` across the cluster is exactly 1."
  - 02-17 acceptance: "A silo does not accept client calls before its cluster join completes and directory cache is primed."
  - 02-18 acceptance: "At least one integration test verifies `len(silo_a.activations) + len(silo_b.activations) == 1` for a shared `GrainId`."
- No Phase 2 task describes an intentional shortcut (e.g. file-based directory substitute, per-silo activation with storage-only serialisation). If any such language exists, it is removed.
- Dependencies listed in each task match the graph in [README.md §Phase 2](README.md#phase-2-dependency-graph). If a task is missing a dependency, add it.

**Deliverable:** edits to the affected Phase 2 task specs. Commit as a single change set titled "Phase 2 spec alignment with single-activation ADR".

#### A.4 — Gap analysis — do we need new tasks?

Answer these explicitly:

- **Gateway forwarding**: Is any new code needed in `GatewayListener` to hand off to the runtime hook, or is the Phase 1 implementation already transparent? Verify: the existing `_dispatch` already hands every request to `runtime.invoke()`. **No new task needed** — silo transparency is already delivered by the runtime hook insertion alone. Capture this finding in task 02-16's description so no future reader wonders whether it was missed.
- **Client changes**: Does `ClusterClient` need any change for multi-silo? It should not — the single-activation contract says clients stay thin. Verify no task assumes otherwise. **No new task needed.**
- **Multi-gateway discovery**: Does the client need to know about more than one gateway? For fault tolerance and load-balancing eventually yes, but not for the single-activation invariant. Out of scope for this plan; flag in the Out-of-scope section if a follow-up is warranted.
- **Storage provider contract**: Does the storage provider need to be aware of cluster membership (e.g. to refuse writes from non-owner silos)? No — the directory guarantees that only the owner's runtime writes to storage for a given grain. Storage remains oblivious. Document this clearly in 02-16 and/or 02-13 descriptions.
- **Missing Phase 2 task?** Review the 21-task list holistically: is any concern in the ADR ungovered? Examples to check:
  - Graceful cluster-leave (silo shutdown triggers directory re-ownership). Task 02-17 lifecycle should cover this in its shutdown stages. Verify; if not, extend 02-17.
  - Directory entry TTL / liveness (entries that point to a silo that's still alive but misbehaving). Covered by failure detector 02-11 + directory recovery 02-15. Verify.
  - Zombie activations after ownership change. Covered by 02-15 + 02-17. Verify.

**Deliverable:** either a short addendum confirming no new task is needed, or the creation of new task-02-NN file(s) for any genuinely missing piece. New tasks inherit the task-template shape and are added to [README.md](README.md) in dependency order.

#### A.5 — Freeze the "ideal task list"

After A.1 – A.4, the [README.md §Phase 2](README.md#phase-2-tasks) task list is the ideal list. Capture this in a short addendum to this task file's Summary section (to be filled at the end of Phase A), stating: "Ideal task list frozen as of commit `<SHA>`. Phase B executes this list verbatim."

---

### Phase B — Code upgrade (execute the ideal task list)

Execute tasks 02-01 through 02-21 in the dependency order from [README.md §Phase 2 dependency graph](README.md#phase-2-dependency-graph). Each task:

- Has its own commit.
- Follows the workflow in [CLAUDE.md](../../CLAUDE.md) §Post-Task Reviews verbatim: implement → tests → code review → security review → fix findings → re-run tests → `ruff` → `pylint` 10.00/10 → `mypy` → fill Summary → commit → mark `[x]` in [README.md](README.md).
- Commit message: `Task 02-NN: <brief imperative summary>`.

Per-layer execution order (respects the graph, allows parallel work where the graph permits):

| Step | Task | Delivers |
|---|---|---|
| 1 | [02-01 Cluster Identity](task-02-01-cluster-identity.md) | `ClusterId`, `silo_id` encoding, stable hash helpers |
| 2 | [02-02 Consistent Hash Ring](task-02-02-consistent-hash-ring.md) | `ConsistentHashRing` with virtual nodes (30/silo) |
| 3 | [02-03 Placement Strategies](task-02-03-placement-strategies.md) | `PlacementStrategy` ABC + `RandomPlacement`, `PreferLocalPlacement` |
| 4 | [02-04 Transport ABCs](task-02-04-transport-abcs.md) | `IClusterTransport`, error hierarchy, `TransportOptions` |
| 5 | [02-05 Wire Protocol](task-02-05-wire-protocol.md) | Frame codec, message types, correlation IDs |
| 6 | [02-09 Membership Table Extensions](task-02-09-membership-table-extensions.md) | `cluster_id`, `gateway_port`, suspicion state on `SiloInfo` |
| 7 | [02-12 Grain Directory Port](task-02-12-grain-directory-port.md) | `IGrainDirectory`, `DirectoryEntry`, `LocalGrainDirectory` |
| 8 | [02-06 Silo Connection](task-02-06-silo-connection.md) | Single multiplexed TCP pair, correlation table, write lock |
| 9 | [02-07 Silo Connection Manager](task-02-07-silo-connection-manager.md) | Per-peer pooling, reconnect-with-backoff, PING/PONG |
| 10 | [02-10 File Locking for Membership](task-02-10-file-locking-membership.md) | OS advisory locks around YAML writes |
| 11 | [02-08 TCP Cluster Transport](task-02-08-tcp-cluster-transport.md) | `TcpClusterTransport` over `INetwork` |
| 12 | [02-11 Failure Detector](task-02-11-failure-detector.md) | Suspicion voting, indirect probes, `IAmAlive` updates |
| 13 | [02-13 Distributed Grain Directory](task-02-13-distributed-grain-directory.md) | Partitioned-by-hash-ring directory, register-if-absent RPC |
| 14 | [02-14 Directory Cache](task-02-14-directory-cache.md) | Local read-through cache, invalidation on `NotOwner` |
| 15 | [02-15 Directory Recovery](task-02-15-directory-recovery.md) | Repair partitions after ownership changes |
| 16 | [02-16 Remote Grain Invocation](task-02-16-remote-grain-invoke.md) | The hook in `GrainRuntime.invoke()`, RPC codec |
| 17 | [02-17 Silo Lifecycle Stages](task-02-17-silo-lifecycle-stages.md) | Ordered startup/shutdown stages |
| 18 | [02-18 Multi-Silo Integration Tests](task-02-18-multi-silo-integration-tests.md) | ~13 scenarios over `InMemoryNetwork` |
| 19 | [02-19 Counter Sample Multi-Silo](task-02-19-counter-sample-multi-silo.md) | Extended `counter_app` CLI, two- and three-silo procedures |
| 20 | [02-20 PostgreSQL Membership](task-02-20-postgresql-membership.md) | Production membership with row-level etag CAS |
| 21 | [02-21 PostgreSQL Storage](task-02-21-postgresql-storage.md) | Production grain-state with etag CAS, optional LISTEN/NOTIFY |

Parallelizable groups (informational — the per-task commits still land one at a time):

- After 02-01: `{02-02, 02-03, 02-04, 02-09}` have no mutual blockers.
- After 02-04 + 02-05: 02-06 and 02-12 have no mutual blockers.
- 02-10 and 02-11 have independent dependency paths after their predecessors.
- 02-14 and 02-15 fan out from 02-13.
- 02-20 and 02-21 (PostgreSQL providers) run as a separate lane from 02-09 onward, fully in parallel with the cluster work. No cluster task depends on them.

#### Key architectural touchpoints (summary)

**The single hook site** — [src/pyleans/pyleans/server/runtime.py](../../src/pyleans/pyleans/server/runtime.py), `GrainRuntime.invoke()`, between args normalization and local activation lookup:

```python
# existing: args/kwargs normalization
# NEW: directory lookup → route to remote silo if not local
if self._directory is not None:
    entry = await self._directory.resolve_or_activate(
        grain_id, self._placement_for(grain_id), caller=self._local_silo
    )
    if entry.silo != self._local_silo:
        return await self._invoke_remote(entry, grain_id, method_name, args, kwargs)
# existing: local activation lookup + enqueue
```

Because this hook sits in the runtime, it covers gateway-received calls, grain-to-grain calls, and programmatic calls — uniformly. Silo transparency is a **consequence of placing the hook here**, not a separate feature.

**Hash ring**: 30 virtual nodes per silo. Hash is SHA-256 truncated to 64 bits over `silo_id:<vnode_index>` for silo positions and `grain_type/grain_key` for grain keys. `owner_of(hash)` walks clockwise. Membership changes rebuild the ring in place.

**Distributed directory**: the ring partitions ownership of directory *entries*. `resolve_or_activate(grain_id)` sends a `REGISTER_IF_ABSENT` RPC to the owner; if it already holds an entry, returns it; otherwise picks a silo via placement and writes. Caller cache invalidates on `NotOwner`.

**Lifecycle**: a silo does not accept client calls before it has joined the cluster, connected to peers, and primed its directory cache. Stages: `BEFORE_INFRASTRUCTURE`, `AFTER_INFRASTRUCTURE`, `BEFORE_CLUSTER_JOIN`, `AFTER_CLUSTER_JOIN`, `BEFORE_ACCEPTING_CALLS`, `AFTER_ACCEPTING_CALLS` (and their shutdown inverses).

**Reuse of Phase 1 primitives** (do **not** rebuild):

- [src/pyleans/pyleans/net/](../../src/pyleans/pyleans/net/) — `INetwork`, `AsyncioNetwork`, `InMemoryNetwork`. `TcpClusterTransport` takes `network: INetwork` so every cluster integration test runs over `InMemoryNetwork` with zero OS ports bound.
- [src/pyleans/pyleans/gateway/protocol.py](../../src/pyleans/pyleans/gateway/protocol.py) — JSON payload schema for grain calls is reused inside cluster-transport frames; framing is new.
- [src/pyleans/pyleans/server/runtime.py](../../src/pyleans/pyleans/server/runtime.py) — single hook site as above.
- [src/pyleans/pyleans/providers/membership.py](../../src/pyleans/pyleans/providers/membership.py) and YAML/markdown providers — extended per 02-09, not rewritten.

---

### Phase C — Verification

When the last Phase B task lands, run the end-to-end verification below. All checks must pass before this task is archived.

#### C.1 — Automated suite

- `pytest` clean across all testpaths. Phase 1 tests (408 at start of Phase 2) continue to pass unchanged; Phase 2 adds new tests per task spec.
- `ruff check .` + `ruff format --check .` — clean.
- `pylint src/pyleans/pyleans src/counter_app src/counter_client` — 10.00/10.
- `mypy .` — clean.

#### C.2 — Single-activation invariant (task 02-18 smoke test)

Two silos wired on a shared `InMemoryNetwork` (no OS ports):

- Client connected to silo A calls `CounterGrain("a").increment()`; runtime resolves directory → routes to owner → returns. Counter = 1.
- Client connected to silo B calls `CounterGrain("a").get_value()` → returns 1 (same grain, same state).
- Assert: `len(silo_a.runtime.activations) + len(silo_b.runtime.activations) == 1` for `CounterGrain/a`.
- Kill the owning silo; next call on the survivor triggers failure detection → directory recovery → reactivation on survivor. State preserved via storage. Activation count still 1.
- Directory cache hit path and `NotOwner` invalidation path both covered.

#### C.3 — Manual localhost run (two silos)

Per task 02-19 procedure:

```bash
# Terminal 1 — silo A
python -m src.counter_app \
    --silo-port 11111 --gateway-port 30000 \
    --cluster-id demo \
    --membership-file ./data/membership.md \
    --storage-dir ./data/storage

# Terminal 2 — silo B (SAME membership and storage)
python -m src.counter_app \
    --silo-port 11112 --gateway-port 30001 \
    --cluster-id demo \
    --membership-file ./data/membership.md \
    --storage-dir ./data/storage

# Terminal 3 — client hits either gateway; sees the same grain
python -m src.counter_client --gateway localhost:30000 inc a   # → 1
python -m src.counter_client --gateway localhost:30001 get a   # → 1 (same grain)
python -m src.counter_client --gateway localhost:30001 inc a   # → 2
python -m src.counter_client --gateway localhost:30000 get a   # → 2
```

Verify:

- `./data/membership.md`: both silos `active`, distinct `silo_id`, matching `cluster_id`.
- Silo logs: exactly one `activate_grain CounterGrain/a` across both processes; the non-owner logs `forward` over cluster transport per call.
- Storage: exactly one `CounterGrain/a.json` with value `2`.
- Kill the owner: after `failure_detector_timeout`, next call triggers reactivation on the survivor; state = `2` preserved.

#### C.4 — Manual localhost run (three silos)

Same procedure with three terminals and three silo-port/gateway-port pairs. Proves the hash ring distributes entries across N > 2 silos.

#### C.5 — ADR compliance pass

Final pass: reread [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) and for each "Consequences" bullet, confirm the implementation matches. Any divergence is either fixed in a follow-up or explicitly documented as a deviation.

---

## Acceptance criteria

- [ ] **Phase A complete**: all ADRs and all task specs in [docs/adr/](../adr/) and [docs/tasks/](.) are internally consistent with [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md). The "ideal task list" is frozen in [README.md §Phase 2](README.md#phase-2-tasks).
- [ ] **Phase B complete**: tasks 02-01 through 02-21 (and any additions from A.4) are `[x]` in [README.md](README.md).
- [ ] **Phase C.1 complete**: the full automated suite is clean.
- [ ] **Phase C.2 complete**: the 02-18 integration smoke test passes, including the single-activation-invariant assertion.
- [ ] **Phase C.3 complete**: the two-silo manual procedure produces the documented output; logs show single activation.
- [ ] **Phase C.4 complete**: the three-silo manual procedure works; hash ring distributes entries.
- [ ] **Phase C.5 complete**: every Consequence bullet in [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) is honoured by the implementation, with explicit confirmation in this task's Summary.
- [ ] This task file is archived: status updated to "completed; historical", Summary section filled, cross-references added to each of tasks 02-01..02-21 pointing back here.

## Findings of code review

_N/A — this is a meta-task; per-task reviews land in each individual task file._

## Findings of security review

_N/A — this is a meta-task; per-task reviews land in each individual task file._

## Summary of implementation

_To be filled in three stages:_

- _After Phase A: record which ADRs/tasks were edited, with commit SHAs. State "Ideal task list frozen as of commit `<SHA>`."_
- _After Phase B: list the 21+ commits (or commit range) for each task. Cross-reference each task's own Summary._
- _After Phase C: record verification results — automated suite pass, manual smoke test output, ADR compliance pass. Declare this task complete._
