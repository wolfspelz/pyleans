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

- [x] **`Silo.__init__` does no I/O.** Every subsystem is constructed
  synchronously; the only network binds (transport, gateway) are
  driven from lifecycle `on_start` hooks.
- [x] **Lifecycle ordering is explicit.** `SiloLifecycle` stage
  assignments (`RUNTIME_INITIALIZE`, `RUNTIME_CLUSTER`,
  `RUNTIME_GRAIN_SERVICES`, `BECOME_ACTIVE`, `ACTIVE`) document
  why runtime stops *after* transport: runtime.stop deactivates
  grains which unregister through the still-running transport.
- [x] **Directory-entry cleanup on graceful stop.** `runtime.stop`
  calls `deactivate_grain` for every activation, which calls
  `_directory.unregister(grain_id, self._local_silo)`. This sends
  a remote unregister RPC to the directory owner while the
  transport is still live.
- [x] **Default `cluster_id=ClusterId("dev")` matches the counter-app
  default.** A silo started with no cluster id still interoperates
  with the sample two-silo walkthrough.
- [x] **Single-silo is the `N = 1` special case.** No code path
  forks on cluster size: the ring has one vnode set for the local
  silo, placement picks local, and the transport's peer list is
  empty so no outbound connects happen.
- [x] **Type hints complete; AAA comments preserved in tests.**
  Every new test in `test_silo_cluster_wiring.py` labels Arrange /
  Act / Assert blocks.
- [x] **Partition injection wraps `INetwork`, not the transport.**
  `_PartitionedNetwork` consults a shared `_PartitionRegistry`
  keyed on ``(source_silo_id, target_silo_id)``; the production
  `TcpClusterTransport` path runs unchanged in every integration
  test.

## Findings of security review

- [x] **No new untrusted deserialization.** The silo composes
  existing wire codecs (`GRAIN_CALL_HEADER`, directory messages,
  membership-snapshot JSON). No new `pickle`, `eval`, or
  user-controlled import.
- [x] **No user input in file paths.** The counter-app CLI already
  sanitises `--membership`, `--storage-dir` via `argparse`; the
  silo forwards them unchanged into the existing provider
  constructors.
- [x] **Lifecycle start-up failures do not leak resources.**
  `SiloLifecycle.start` rolls back every completed stage on
  failure; the silo's new wiring surfaces that directly — a
  transport bind failure rolls back membership registration, a
  gateway bind failure rolls back the agent / transport.
- [x] **No unbounded resource consumption added.** Outbound peer
  connects at startup use `_PEER_CONNECT_TIMEOUT = 2.0s` via
  `asyncio.wait_for`; failures are logged and swallowed so one
  unreachable peer cannot block cluster join.
- [x] **Atexit unregister is best-effort and bounded.** The atexit
  hook runs `_unregister_from_membership` inside a fresh event
  loop with its existing OCC retry ceiling
  (`_OCC_MAX_RETRIES = 5`); exceptions are logged and cannot hang
  interpreter shutdown.
- [x] **No credentials or grain state logged.** Silo log lines
  carry silo_id, stage number, and counts only; all state / RPC
  payload logging continues to be DEBUG-level and redacted per
  adr-logging.

## Summary of implementation

### Files created

- `src/pyleans/test/integration/test_silo_cluster_wiring.py` — 9
  new integration scenarios covering the task's acceptance criteria
  (N=1 happy path, N=2 single-activation invariant, N=3 ring
  distribution, concurrent resolve_or_activate, partitioned caller,
  stale-cache eviction, graceful owner leave + survivor
  reactivation, per-key isolation, heal-after-partition).

### Files modified

- `src/pyleans/pyleans/server/silo.py` — full rewrite. `__init__`
  composes `TcpClusterTransport`, `DistributedGrainDirectory`,
  `DirectoryCache`, `FailureDetector`, `MembershipAgent`,
  `GrainRuntime`, and `GatewayListener` with no I/O, then
  subscribes each subsystem to `SiloLifecycle`. `start` /
  `start_background` / `stop` delegate to the lifecycle engine.
  Heartbeating moved to the membership agent's `i_am_alive_loop`
  (the old silo-level `_heartbeat_task` + `_HEARTBEAT_INTERVAL`
  are gone).
- `src/pyleans/test/integration/harness.py` — replaces
  `HarnessSilo` / `_FabricTransport`. `ClusterHarness` now builds
  N real `Silo` instances over one shared `InMemoryNetwork`.
  `_PartitionedNetwork` + `_PartitionRegistry` implement
  partition injection by wrapping `INetwork` at `open_connection`.
  Fixed epoch=1 per silo so the hash ring is deterministic across
  test runs.
- `src/pyleans/test/test_silo.py` — heartbeat tests now tune
  `FailureDetectorOptions.i_am_alive_interval` instead of the
  removed `_HEARTBEAT_INTERVAL` module constant.
- `src/counter_app/main.py` — passes `ClusterId(args.cluster_id)`
  through to `Silo(...)` so the CLI's `--cluster-id` flag actually
  reaches the cluster transport.
- `src/counter_app/README.md` — replaces the "partially blocked"
  caveat with an expected log excerpt from a live two-silo run.
- `docs/tasks/task-02-17-silo-lifecycle-stages.md` — "Closed by
  task 02-22" section added to the Summary.
- `docs/tasks/task-02-18-multi-silo-integration-tests.md` — "Harness
  migrated to real Silo in task 02-22" section added to the
  Summary.
- `docs/tasks/task-02-19-counter-sample-multi-silo.md` — replaced
  the "partially blocked" deviation with completion language
  cross-referencing task 02-22.
- `src/pyleans/test/test_reference.py`, `src/pyleans/test/test_runtime.py`
  — import order auto-fixed by `ruff --fix` (pre-existing warnings).

### Key decisions

- **Runtime stage > transport stage.** Previously I had runtime
  below transport (stop reverses the start order). That broke
  graceful shutdown: `runtime.stop` needs the transport alive to
  unregister directory entries. Placing runtime at
  `RUNTIME_GRAIN_SERVICES (8000)` — above
  `RUNTIME_CLUSTER (7000)` — lets stop bring gateway → agent →
  runtime → transport → (no-op register) down in the right order.
- **Directory + runtime always composed.** No `LocalGrainDirectory`
  fallback. `DistributedGrainDirectory` with `initial_ring = [local]`
  behaves exactly like the single-silo directory so there is one
  code path for all N.
- **Heartbeat driven by the membership agent, not the silo.**
  The silo's pre-refactor `_heartbeat_task` duplicated what the
  agent's `_i_am_alive_loop` already does. Collapsing them means
  one heartbeat cadence, controlled via `FailureDetectorOptions`.
- **Partition injection wraps `INetwork`.** The task spec called
  for this explicitly. Consequence: the real `TcpClusterTransport`
  / `SiloConnectionManager` paths run in every integration test.
- **Fixed epoch in the harness.** Production uses
  `epoch=int(time.time())`. Under that scheme, the hash ring
  shifts every run because `silo_id = host:port:epoch` changes,
  which made cross-silo placement assertions flaky. The harness
  pins `epoch=1` per silo so ring decisions are deterministic.
  Production code still defaults to wall-clock.

### Deviations

- The task's Phase C verification of
  `docs/tasks/task-02-single-activation-fix.md` is not filled in:
  that file has been removed in commit `13b2383` as part of the
  spec consolidation. Phase C equivalents are now the 14
  integration scenarios in `test_cluster_basics.py`,
  `test_failure_scenarios.py`, and
  `test_silo_cluster_wiring.py`.
- The task sketched `MembershipAgent.announce_joining()` /
  `MembershipAgent.announce_leaving()` as lifecycle methods. The
  existing `MembershipAgent` has no such methods; the silo keeps
  the OCC `_register_in_membership` / `_unregister_from_membership`
  helpers inherited from Phase 1 and delegates runtime heartbeating
  to the agent's own `_i_am_alive_loop`.
- `DistributedGrainDirectory.start()` / `DirectoryRecovery.start()`
  were referenced in the design sketch but do not exist — the
  directory and recovery coordinator are passive objects that
  react to membership changes via `on_membership_changed`. The
  silo wires them at `RUNTIME_CLUSTER` by calling
  `on_membership_changed` directly after the transport binds.
- A graceful-leave survivor recovery scenario
  (`TestSiloLeavesDuringCall`) has to explicitly invoke
  `on_membership_changed` + `cache.invalidate_all()` on the
  survivor in the test. In production this is driven by
  membership-snapshot broadcasts from the agent; in an
  in-process harness the broadcast cadence is slow relative to
  test wall-clock, so the test short-circuits it.

### Test coverage

- Full fast-test suite: **823 passing** (unchanged from pre-refactor
  baseline — the silo rewrite plus heartbeat-test update kept the
  count stable).
- Integration suite: **14 passing** — 4 from
  `test_cluster_basics.py`, 1 from `test_failure_scenarios.py`, 9
  new from `test_silo_cluster_wiring.py` — opt-in via
  `pytest -m integration`.
- `ruff check .` clean; `ruff format --check .` clean;
  `pylint src` 10.00/10; `mypy .` has 66 pre-existing errors
  (down from 76 at task start) and zero new errors in the files
  this task touched.
