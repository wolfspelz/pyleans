# Task 02-single-activation-fix: Phase 2 meta-guide — real virtual-actor cluster

> **Status: temporary.** This file exists to give Phase 2 a single readable entry point while tasks 02-01 through 02-21 are still spec-only. It supersedes itself once the final Phase 2 task lands — at that point it becomes archival (keep the file for history; cross-reference from it to the `[x]` entries in [README.md](README.md)).
>
> **Coding rules**: Follow [CLAUDE.md](../../CLAUDE.md), [.claude/coding-rules.md](../../.claude/coding-rules.md), and [.claude/testing-rules.md](../../.claude/testing-rules.md) strictly.

## Dependencies

- All of Phase 1 (tasks 01-01 through 01-21) — complete.
- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) — the contract this task implements.

## Supersedes / superseded by

- Supersedes: none.
- Superseded by: tasks 02-01 through 02-21 collectively; when all are `[x]`, archive this file.

## References

- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) — decision that drives this task.
- [adr-grain-directory](../adr/adr-grain-directory.md) — consistency model and hash-ring choice.
- [adr-cluster-transport](../adr/adr-cluster-transport.md) — pluggable cluster mesh.
- [adr-cluster-access-boundary](../adr/adr-cluster-access-boundary.md) — gateway is sole external entry.
- [adr-network-port-for-testability](../adr/adr-network-port-for-testability.md) — `INetwork` foundation the transport rides on.
- [plan.md §Phase 2](../plan.md) — cluster scope.
- [README.md](README.md) — the 21 individual Phase 2 tasks and their dependency graph.

## Description

Phase 1 ships a single silo with an in-memory grain directory and no inter-silo transport. A naïve multi-silo run (two silos + split storage, or two silos + file-based directory workaround) breaks the single-activation invariant defined in [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md). Phase 2 implements the full cluster stack so that:

- Every silo participates in a consistent-hash-ring-partitioned `IGrainDirectory`.
- `GrainRuntime.invoke()` consults the directory and forwards remote calls over `IClusterTransport`.
- Any gateway port is a valid cluster entry for external clients — silo transparency is a structural property of the runtime hook site, not a separate feature.
- The YAML membership provider is hardened with locking, extended with `cluster_id`/`gateway_port`/suspicion state, and paired with a PostgreSQL provider for production deployments.

This task is the **meta-guide** that sequences the 21 individual Phase 2 task files. Each of those has its own full spec; this file exists only to let a reader see the whole shape at once without reading 21 files in the wrong order.

## Execution order

Follow the dependency graph in [README.md §Phase 2](README.md#phase-2-dependency-graph). Execute each task in its own commit after the mandatory code + security reviews per [CLAUDE.md](../../CLAUDE.md) §Post-Task Reviews. Do **not** batch multiple tasks per commit.

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

### Parallelizable groups

- After 02-01: `{02-02, 02-03, 02-04, 02-09}` run in parallel.
- After 02-04 + 02-05: 02-06 and 02-12 have no mutual blocker.
- 02-10 and 02-11 have independent dependency paths after their predecessors.
- 02-14 and 02-15 fan out from 02-13.
- 02-20 and 02-21 form a separate lane that can run in parallel with the cluster tasks from 02-09 onward. No cluster task depends on them.

## Key architectural touchpoints

### The single hook site

One insertion point in [src/pyleans/pyleans/server/runtime.py](../../src/pyleans/pyleans/server/runtime.py), `GrainRuntime.invoke()`, between args normalization and local activation lookup:

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

Because this hook sits in the runtime (not in `GatewayListener`), it covers gateway-received client calls, grain-to-grain calls, and programmatic calls — uniformly. **Silo transparency is a consequence of the architecture, not a separate feature.**

### Hash ring

30 virtual nodes per silo. Hash function is SHA-256 truncated to 64 bits over `silo_id:<vnode_index>` for silo positions and `grain_type/grain_key` for grain keys. `owner_of(hash)` walks clockwise, returning the first silo whose arc contains the hash. Membership changes re-materialize the ring in place; each silo subscribes to membership updates and rebuilds.

### Distributed directory

The ring partitions ownership of directory **entries** — the silo whose arc contains `hash_grain_id(grain_id)` owns the entry. `resolve_or_activate(grain_id)` sends a `REGISTER_IF_ABSENT` RPC to the owner:

- If the owner already has an entry, it returns that entry.
- Otherwise, it picks a silo via the task's `PlacementStrategy` and writes the entry.

The caller's local cache stores the result. On a `NotOwner` response (ring changed between cache read and call), the cache invalidates and the call retries.

### Lifecycle

A silo must not accept client calls until it has: joined the cluster, connected to peers, and primed its directory cache. Task 02-17 defines stages `BEFORE_INFRASTRUCTURE`, `AFTER_INFRASTRUCTURE`, `BEFORE_CLUSTER_JOIN`, `AFTER_CLUSTER_JOIN`, `BEFORE_ACCEPTING_CALLS`, `AFTER_ACCEPTING_CALLS` and their shutdown inverses. `Silo.start()`/`stop()` are rewritten to drive the lifecycle.

### Reuse of Phase 1 primitives

Do **not** rebuild:

- [src/pyleans/pyleans/net/](../../src/pyleans/pyleans/net/) — `INetwork`, `AsyncioNetwork`, `InMemoryNetwork`. `TcpClusterTransport` takes `network: INetwork` so every cluster integration test runs over `InMemoryNetwork` with zero OS ports bound (per [.claude/testing-rules.md](../../.claude/testing-rules.md) §2.4).
- [src/pyleans/pyleans/gateway/protocol.py](../../src/pyleans/pyleans/gateway/protocol.py) — JSON payload schema for grain calls is reused inside cluster-transport frames; the framing is new (cluster wire adds handshake, correlation, one-way).
- [src/pyleans/pyleans/server/runtime.py](../../src/pyleans/pyleans/server/runtime.py) — single hook site as above.
- [src/pyleans/pyleans/providers/membership.py](../../src/pyleans/pyleans/providers/membership.py) and the YAML/markdown providers — extended per 02-09, not rewritten.

## Files touched

### Phase 1 files modified during Phase 2

- [src/pyleans/pyleans/server/runtime.py](../../src/pyleans/pyleans/server/runtime.py) — constructor takes `directory`/`cluster_transport`; `invoke()` gains the routing hook.
- [src/pyleans/pyleans/server/silo.py](../../src/pyleans/pyleans/server/silo.py) — constructs cluster subsystem; `start()`/`stop()` in terms of lifecycle stages.
- [src/pyleans/pyleans/identity.py](../../src/pyleans/pyleans/identity.py) — add `ClusterId`, `silo_id` property, stable hash helpers.
- [src/pyleans/pyleans/providers/membership.py](../../src/pyleans/pyleans/providers/membership.py) — extend `SiloInfo`.
- [src/pyleans/pyleans/gateway/listener.py](../../src/pyleans/pyleans/gateway/listener.py) — **no change needed** (`_dispatch` already goes through `runtime.invoke()`).
- [src/counter_app/main.py](../../src/counter_app/main.py) — CLI args per task 02-19.
- [src/pyleans/pyleans/server/providers/yaml_membership.py](../../src/pyleans/pyleans/server/providers/yaml_membership.py) — file locking per task 02-10.

### New source trees

- `src/pyleans/pyleans/cluster/` — `identity.py`, `hash_ring.py`, `placement.py`, `directory.py`, `distributed_directory.py`, `directory_cache.py`, `directory_recovery.py`, `failure_detector.py`.
- `src/pyleans/pyleans/transport/` — `errors.py`, `messages.py`, `cluster.py` (IClusterTransport), `wire.py`.
- `src/pyleans/pyleans/transport/tcp/` — `connection.py`, `connection_manager.py`, `transport.py`.
- `src/pyleans/pyleans/server/remote_invoke.py` — RPC codec + `_invoke_remote()`.
- `src/pyleans/pyleans/server/lifecycle.py` — stages, `SiloLifecycle`.
- `src/pyleans/pyleans/server/providers/postgres_membership.py`, `postgres_storage.py`.

### New tests

- Unit test per new module under `src/pyleans/test/`, AAA-labeled per [.claude/testing-rules.md](../../.claude/testing-rules.md).
- `src/pyleans/test/integration/test_cluster_*.py` — 02-18 scenarios, all over `InMemoryNetwork`.
- 02-20/02-21 tests use testcontainers-postgres, explicitly marked and skippable.

## Acceptance criteria (this meta-task)

- [ ] All 21 Phase 2 tasks marked `[x]` in [README.md](README.md).
- [ ] Phase 1 test suite passes unchanged (408 tests as of Phase 1 close).
- [ ] Phase 2 integration smoke test from task 02-18 passes: two silos on shared `InMemoryNetwork`, client routes through either gateway, single activation invariant verifiable from activation counts.
- [ ] Manual two-silo localhost run (procedure in task 02-19) yields the expected counter sequence; membership file shows both silos `active`; storage shows exactly one `CounterGrain/a.json`.
- [ ] Manual three-silo localhost run works — verifies hash ring distributes across N>2.
- [ ] `ruff`, `pylint` (10.00/10), `mypy` clean across the whole repo.
- [ ] This file (the meta-task) is archived (status updated, cross-reference added to the Phase 2 task entries).

## Findings of code review

_N/A — this is a meta-task; per-task reviews land in each task file._

## Findings of security review

_N/A — this is a meta-task; per-task reviews land in each task file._

## Summary of implementation

_To be filled when all 21 Phase 2 tasks are complete. Will cross-reference each task's own Summary section._
