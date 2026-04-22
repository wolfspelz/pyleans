# Task 02-22: Silo Cluster Wiring

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-13-distributed-grain-directory.md](task-02-13-distributed-grain-directory.md)
- [task-02-14-directory-cache.md](task-02-14-directory-cache.md)
- [task-02-15-directory-recovery.md](task-02-15-directory-recovery.md)
- [task-02-16-remote-grain-invoke.md](task-02-16-remote-grain-invoke.md)
- [task-02-17-silo-lifecycle-stages.md](task-02-17-silo-lifecycle-stages.md)
- [task-02-18-multi-silo-integration-tests.md](task-02-18-multi-silo-integration-tests.md)

## References
- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) -- the contract this task finally delivers end-to-end in the real `Silo`.
- [adr-cluster-transport](../adr/adr-cluster-transport.md)
- [adr-grain-directory](../adr/adr-grain-directory.md)
- [task-02-single-activation-fix.md](task-02-single-activation-fix.md) §Phase C.3/C.4 -- the two-silo and three-silo manual procedures this task must make pass.
- [plan.md §Phase 2](../plan.md)

## Description

Tasks 02-13 through 02-18 delivered every Phase 2 cluster primitive (hash ring, distributed directory, directory cache, directory recovery, failure detector, remote-invoke hook, lifecycle engine) but deliberately deferred the `Silo` class refactor that assembles them:

- [task-02-17](task-02-17-silo-lifecycle-stages.md) Summary: *"The Silo class is not refactored to drive the lifecycle today... Rather than risk a partial-broken state, we ship the engine and let 02-18 do the migration under multi-silo test coverage."*
- [task-02-18](task-02-18-multi-silo-integration-tests.md) Summary: *"Harness assembles components, not Silos. Silo's own lifecycle refactor is deferred (task 02-17 Summary)."*
- [task-02-19](task-02-19-counter-sample-multi-silo.md) Summary: *"partially blocked: the Silo class itself is not yet wired to distribute grains across the cluster... end-to-end cluster routing awaits the Silo refactor."*

The consequence is a real bug in the two-silo walkthrough: each silo wires `LocalGrainDirectory` at [src/pyleans/pyleans/server/silo.py:94](../../src/pyleans/pyleans/server/silo.py), so two silos sharing a membership file still produce two independent activations of the same `GrainId`. Set/get appear consistent only because both silos share `./data/storage` and each activation reads once at activation time -- stale in-memory state surfaces on the second round-trip.

This task finishes the deferred refactor: `Silo.__init__` composes cluster transport + distributed directory + directory cache + failure detector + directory recovery, driven by the `SiloLifecycle` engine. `ClusterHarness` then drops its hand-assembled `HarnessSilo` in favour of real `Silo` instances so the integration suite actually exercises the production construction path.

### Motivation

Three existing task summaries acknowledge this gap. The meta-plan [task-02-single-activation-fix.md](task-02-single-activation-fix.md) Phase C verification cannot be signed off until this task lands. Until then, the documented two-silo walkthrough is aspirational, and a user running it hits divergent state without any warning.

### Files to create/modify

**Modify:**
- `src/pyleans/pyleans/server/silo.py` -- replace direct `LocalGrainDirectory` wiring with a lifecycle-driven composition of cluster components. Single-silo deployments still work (ring of one, local is always the owner).
- `src/pyleans/test/integration/harness.py` -- replace `HarnessSilo` / `_FabricTransport` hand-assembly with real `Silo` instances constructed over `InMemoryNetwork`. Keep partition-injection by wrapping `INetwork`, not by forking transport.
- `src/counter_app/README.md` -- remove the "partially blocked" caveat; the documented walkthrough now matches reality.
- `docs/tasks/task-02-17-silo-lifecycle-stages.md` -- append a "Closed by task 02-22" note to the Summary.
- `docs/tasks/task-02-18-multi-silo-integration-tests.md` -- append a "Harness migrated to real Silo in task 02-22" note to the Summary.
- `docs/tasks/task-02-19-counter-sample-multi-silo.md` -- replace the "partially blocked" Summary text with a completion note that cross-references this task.
- `docs/tasks/task-02-single-activation-fix.md` -- fill in Phase B final entry and execute Phase C verification (C.1 automated suite, C.2 integration smoke test, C.3 two-silo manual, C.4 three-silo manual, C.5 ADR compliance).
- Tests for all created/modified files.

No new production modules: every component this task needs already exists under [src/pyleans/pyleans/cluster/](../../src/pyleans/pyleans/cluster/) and [src/pyleans/pyleans/transport/](../../src/pyleans/pyleans/transport/).

### Design

#### Silo construction shape

```python
# src/pyleans/pyleans/server/silo.py (sketch -- bodies omitted)

class Silo:
    def __init__(
        self,
        grains: list[type],
        storage_providers: dict[str, StorageProvider] | None = None,
        membership_provider: MembershipProvider | None = None,
        stream_providers: dict[str, StreamProvider] | None = None,
        port: int = _DEFAULT_PORT,
        gateway_port: int = _DEFAULT_GATEWAY_PORT,
        host: str = _DEFAULT_HOST,
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
        *,
        cluster_id: ClusterId = ClusterId("dev"),
        placement: PlacementStrategy | None = None,
        network: INetwork | None = None,
    ) -> None: ...

    async def start(self) -> None:
        """Run the lifecycle to RUNTIME_ACCEPTING_CALLS."""
        ...
```

Internal composition performed by `__init__` (no I/O) and wired as lifecycle participants (all I/O):

| Stage (from `LifecycleStage`) | Participant action |
|---|---|
| `BEFORE_INFRASTRUCTURE` | `MembershipAgent.announce_joining()` |
| `AFTER_INFRASTRUCTURE` | `TcpClusterTransport.start()` -- binds `host:port` via `INetwork` |
| `BEFORE_CLUSTER_JOIN` | `MembershipAgent` transitions to `active`; `FailureDetector.start()` |
| `AFTER_CLUSTER_JOIN` | `SiloConnectionManager` establishes outbound connections; `ConsistentHashRing.rebuild(active_silos)` |
| `BEFORE_ACCEPTING_CALLS` | `DistributedGrainDirectory.start()`; `DirectoryCache` is now populated on-demand; `DirectoryRecovery.start()` |
| `AFTER_ACCEPTING_CALLS` | `GatewayListener.start()` (last, so external traffic only arrives after the rest is ready) |

Shutdown inverts: `GatewayListener` stops first, `MembershipAgent.announce_leaving()` runs last after directory entries have been unregistered (graceful-leave contract per task 02-17).

Single-silo deployments are the N=1 special case: membership shows one silo, the ring has one owner, every grain resolves to local. No code path forks on N.

#### Harness migration

```python
# src/pyleans/test/integration/harness.py (sketch)

class ClusterHarness:
    def __init__(self, n: int, *, grains: list[type]) -> None: ...
    async def start(self) -> None:
        """Construct N real Silo instances over one InMemoryNetwork; start them concurrently."""
    async def partition(self, a: int, b: int) -> None:
        """Drop packets between silos[a] and silos[b] on the shared InMemoryNetwork."""
```

Partition injection moves from `_FabricTransport.is_dropped` to an `InMemoryNetwork`-level filter, since real `TcpClusterTransport` now sits between the harness and the fabric. Every existing integration test must continue to pass unchanged -- they are the regression net.

#### Counter-app consequences

No code change in `src/counter_app/` -- the sample already passes `host`, `port`, `gateway_port`, `membership_provider`, and `storage_providers` to `Silo`. Only `src/counter_app/README.md` needs the "partially blocked" note removed, and a Phase C.3-style expected-log excerpt added so a reader can verify single-activation locally.

### Acceptance criteria

- [ ] A `Silo` constructed with default arguments (single-silo deployment) starts, serves grain calls, and passes the existing Phase 1 test suite unchanged.
- [ ] Two `Silo` instances sharing a membership file and cluster id enforce single activation: for any `GrainId`, `len(silo_a.runtime.activations) + len(silo_b.runtime.activations) == 1` once the first call completes.
- [ ] The manual two-silo walkthrough in [task-02-single-activation-fix §C.3](task-02-single-activation-fix.md) produces the documented output verbatim: `inc` via either gateway returns increasing values; `get` via the other gateway returns the same value after the `inc`.
- [ ] The manual three-silo walkthrough in [task-02-single-activation-fix §C.4](task-02-single-activation-fix.md) works; hash ring distributes grain ownership across silos (assertable via per-silo `grain_count` in `info`).
- [ ] Killing the owning silo causes the next call on a survivor to trigger reactivation on the survivor with state preserved via shared storage; `activation_count == 1` holds across the cluster again after recovery.
- [ ] `ClusterHarness` constructs real `Silo` instances; `HarnessSilo` and `_FabricTransport` are removed. All 13 existing integration scenarios in [task 02-18](task-02-18-multi-silo-integration-tests.md) pass unchanged.
- [ ] Unit tests cover happy path, equivalence classes (N=1, N=2, N=3), boundaries (silo leaves during call), error cases (peer unreachable, directory `NotOwner` on stale cache), and edge cases (concurrent `resolve_or_activate` from two silos).
- [ ] `ruff check .`, `ruff format --check .`, `pylint src` (10.00/10), `mypy .` all clean.
- [ ] All existing tests still pass.
- [ ] [task-02-single-activation-fix.md](task-02-single-activation-fix.md) Phase C is signed off in its Summary section.

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Files created
- _TBD_

### Files modified
- _TBD_

### Key decisions
- _TBD_

### Deviations
- _TBD_

### Test coverage
- _TBD_
