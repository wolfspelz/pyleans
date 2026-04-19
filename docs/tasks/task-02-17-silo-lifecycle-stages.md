# Task 02-17: Silo Lifecycle Stages -- Ordered Startup, Connectivity Validation, Reverse-Order Shutdown

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-08-tcp-cluster-transport.md](task-02-08-tcp-cluster-transport.md)
- [task-02-09-membership-table-extensions.md](task-02-09-membership-table-extensions.md)
- [task-02-11-failure-detector.md](task-02-11-failure-detector.md)
- [task-02-13-distributed-grain-directory.md](task-02-13-distributed-grain-directory.md)
- [task-02-14-directory-cache.md](task-02-14-directory-cache.md)

## References
- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) -- ordered stages are what make "no client calls accepted before cluster join + directory prime" enforceable; without them, a silo could answer gateway requests and activate grains before it had agreed with the cluster that it owns its share of the ring.
- [adr-graceful-shutdown](../adr/adr-graceful-shutdown.md)
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §8 silo lifecycle (stages table, connectivity validation, IAmAlive staleness skip)
- [plan.md](../plan.md) -- Phase 2 item 9

## Description

Phase 1 `Silo.start()` does everything inline in one coroutine: register grain classes, initialize providers, start runtime, register in membership, start heartbeat, listen for gateway, wait for stop. That works for a single process but does not compose when the cluster adds networking, a failure detector, and a distributed directory -- each of which has ordering constraints (transport must be listening before the silo announces itself; failure detector must not probe before the ring has membership; grains must not accept calls until the cluster has agreed the silo is Active).

Orleans solves this with numeric **lifecycle stages**. Components register themselves at a stage; startup runs stages in ascending order, shutdown in descending order. This task introduces the same model, wires every Phase 2 subsystem at the right stage, and adds the two critical cluster-joining behaviors: **connectivity validation** and **stale-IAmAlive skip**.

### Files to create/modify

**Create:**
- `src/pyleans/pyleans/server/lifecycle.py` -- `SiloLifecycle`, `LifecycleStage` constants, `LifecycleParticipant` protocol

**Modify:**
- `src/pyleans/pyleans/server/silo.py` -- refactor `start()` / `stop()` to drive the lifecycle

### Stages

Numeric constants so custom subsystems can wedge in between framework stages without touching core:

```python
class LifecycleStage(IntEnum):
    FIRST                   = -1_000_000
    RUNTIME_INITIALIZE      = 2_000   # logging, DI container, event loop config
    RUNTIME_SERVICES        = 4_000   # transport.start() listening for inbound; gateway listener
    RUNTIME_STORAGE         = 6_000   # storage providers online
    RUNTIME_CLUSTER         = 7_000   # membership provider read, ring built, directory created
    RUNTIME_GRAIN_SERVICES  = 8_000   # grain runtime, timer registry, placement registry
    APPLICATION_SERVICES    = 10_000  # user-registered services
    BECOME_ACTIVE           = 19_999  # connectivity validation + table write with Active status
    ACTIVE                  = 20_000  # ready to accept grain calls; failure detector running
    LAST                    = 1_000_000
```

Matches Orleans §8.1 semantics with names adapted to pyleans subsystem structure. Values deliberately spaced to leave room for user insertion.

### `SiloLifecycle`

```python
class LifecycleParticipant(Protocol):
    async def on_start(self, stage: int) -> None: ...
    async def on_stop(self, stage: int) -> None: ...


class SiloLifecycle:
    def subscribe(self, stage: int, participant: LifecycleParticipant) -> None: ...

    async def start(self) -> None:
        """Run on_start(stage) on all subscribed participants, in ascending
        stage order. Participants in the same stage run concurrently; the
        stage completes only when all participants for that stage have
        finished.

        If any participant raises, remaining participants in that stage
        complete, then startup aborts: an inverse cleanup runs on_stop for
        every stage that already completed, in descending order."""

    async def stop(self) -> None:
        """Run on_stop(stage) in descending stage order. Exceptions are
        logged but do not interrupt shutdown — stopping must be best-effort."""
```

Concurrent-within-stage means independent providers (storage + stream) can parallelize their init. Sequential-across-stages gives us the ordering guarantee.

### Participant registration

Silo registers every subsystem at its appropriate stage during construction. Example:

```python
lifecycle.subscribe(LifecycleStage.RUNTIME_SERVICES, self._transport)
lifecycle.subscribe(LifecycleStage.RUNTIME_STORAGE,  _provider_list_participant(self._storage))
lifecycle.subscribe(LifecycleStage.RUNTIME_CLUSTER,  self._directory)
lifecycle.subscribe(LifecycleStage.RUNTIME_CLUSTER,  self._directory_cache)
lifecycle.subscribe(LifecycleStage.RUNTIME_GRAIN_SERVICES, self._runtime)
lifecycle.subscribe(LifecycleStage.BECOME_ACTIVE,    self._join_cluster_participant)
lifecycle.subscribe(LifecycleStage.ACTIVE,           self._membership_agent)
```

### `BECOME_ACTIVE` -- the critical stage

This stage does three things in sequence:

1. **Read the table** to get the current active silo set.
2. **Skip stale silos** -- any silo whose `i_am_alive` is older than `num_missed_table_i_am_alive_limit * i_am_alive_interval` is presumed crashed and gets **no** connectivity check (otherwise a single forgotten row would block all cluster startups). Matches Orleans §3.10.
3. **Validate two-way connectivity** to every remaining active silo:
   - For each active peer: `await transport.connect_to_silo(peer)`, then `await transport.send_ping(peer, timeout=handshake_timeout)`. Both must succeed.
   - If any peer fails: abort startup. Log ERROR, run shutdown for stages that completed, exit with non-zero status. The supervisor can retry.
4. **Write our row with status=ACTIVE** via `try_update_silo`. Atomic with version bump.

Orleans enforces this (§2 step 3) to prevent asymmetric connectivity splits where A thinks B is alive but B cannot see A -- such asymmetry leads to false death declarations. A silo that cannot be reached by every existing member has no business joining.

### Exception: single silo

If the active silo set (after staleness skip) is empty, connectivity validation is trivially satisfied and the silo proceeds as the first cluster member. This is the common dev-mode case.

### Shutdown sequence

`SiloLifecycle.stop()` runs stages in **descending** order:

1. `ACTIVE` → `MembershipAgent.on_stop`: write `status=SHUTTING_DOWN`, stop probe loop.
2. `BECOME_ACTIVE` → mark row `DEAD`, snapshot-broadcast the change (best-effort).
3. `APPLICATION_SERVICES` → user shutdown.
4. `RUNTIME_GRAIN_SERVICES` → deactivate every local grain (calls `on_deactivate`, saves state via `write_state`), unregister from directory.
5. `RUNTIME_CLUSTER` → stop directory, cache, ring.
6. `RUNTIME_STORAGE` → close storage providers.
7. `RUNTIME_SERVICES` → stop transport (closes all connections), stop gateway.
8. `RUNTIME_INITIALIZE` → teardown logging, DI container.

Each `on_stop` has a bounded timeout (`options.stage_stop_timeout`, default 5 s). Exceeded → log ERROR, force-close, continue. We prioritize process exit over hanging on a misbehaving subsystem; the supervisor will restart us.

### SIGTERM handling preserved

The Phase 1 signal-handler mechanism ([silo.py:269-287](../../src/pyleans/pyleans/server/silo.py#L269-L287)) stays. The signal handler now calls `lifecycle.stop()` instead of the inline shutdown logic.

### Acceptance criteria

- [ ] Stages execute in ascending order on `start()`, descending on `stop()`
- [ ] Participants in the same stage run concurrently (test with two participants that each sleep; total time ≈ max(sleep), not sum)
- [ ] `BECOME_ACTIVE` pings every non-stale active silo; aborts on any failure
- [ ] Stale `i_am_alive` rows are skipped during connectivity validation
- [ ] Single-silo startup succeeds when the table is empty
- [ ] Startup failure at stage N runs on_stop for stages < N in descending order
- [ ] Shutdown continues even if a participant raises in on_stop
- [ ] Shutdown stage timeout forces continuation
- [ ] Phase 1 `Silo` still passes its existing tests after the refactor
- [ ] Integration test: 2-silo cluster, first silo starts, second silo starts and joins via connectivity validation
- [ ] Integration test: simulated asymmetric partition -- second silo aborts startup with a clear error
- [ ] **Call-before-ACTIVE is rejected**: a gateway request arriving at a silo before it reaches the `ACTIVE` stage is rejected cleanly (error code or connection refusal); no grain is activated before cluster join and directory cache priming complete. Upholds [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) — a silo must not claim ownership of any grain before the cluster has agreed it is Active.
- [ ] **Graceful-leave cleanup**: on shutdown, the silo unregisters its directory entries (via `IGrainDirectory.unregister`) before transport/cluster stages tear down, so surviving silos do not have to rebuild ownership from scratch for grains this silo hosted.

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
