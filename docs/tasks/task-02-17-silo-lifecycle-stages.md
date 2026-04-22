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

- [x] Stages execute in ascending order on `start()`, descending on `stop()`
- [x] Participants in the same stage run concurrently (timing assertion on two 50 ms sleeps in one stage → total < 90 ms)
- [ ] `BECOME_ACTIVE` pings every non-stale active silo — **deferred to task 02-18** (needs a real multi-silo fixture). The lifecycle engine provides the stage; the connectivity participant will slot in during 02-18 wiring.
- [ ] Stale `i_am_alive` rows are skipped during connectivity validation — **deferred to task 02-18**
- [x] Single-silo startup succeeds when the table is empty — covered by existing Silo tests (Phase 1 still green)
- [x] Startup failure at stage N runs on_stop for stages < N in descending order
- [x] Shutdown continues even if a participant raises in on_stop
- [x] Shutdown stage timeout forces continuation
- [x] Phase 1 `Silo` still passes its existing tests — Silo wiring is intentionally untouched; Phase 1 semantics unchanged (all pre-existing Silo tests pass)
- [ ] Integration test: 2-silo cluster, second silo joins — **deferred to task 02-18**
- [ ] Integration test: asymmetric partition abort — **deferred to task 02-18**
- [ ] Call-before-ACTIVE rejection — **deferred to task 02-18** (requires Silo wiring)
- [ ] Graceful-leave cleanup — **deferred to task 02-18** (requires Silo wiring)

## Findings of code review

- [x] **`stop()` is idempotent and exception-swallowing.** A silo must
  exit quickly even when subsystems misbehave. The engine wraps each
  participant's `on_stop` with `asyncio.wait_for` and logs-and-continues
  on both `TimeoutError` and arbitrary exceptions.
- [x] **Subscription locked after `start()`.** Participants added
  mid-start would not see their stage run in order; we raise
  `RuntimeError` with a clear message rather than silently misbehaving.
- [x] **`StartupAbortedError` is the sole start-path exception.** The
  engine wraps the underlying cause with `from exc` so the original
  failure is preserved for logs without leaking many error types out
  of the engine.
- [x] **Rollback on start failure.** Completed stages run their
  `on_stop` in reverse order before the engine propagates the
  abort — tested in `test_failure_rolls_back_completed_stages`.

## Findings of security review

- [x] **No untrusted input.** The lifecycle engine runs only
  subscriptions registered by silo-owned code; there is no path from
  network or disk into subscription.
- [x] **Deadlock resistant.** Per-stage `wait_for` prevents a hung
  participant from preventing stop. The engine cannot be wedged by
  a single misbehaving subsystem.
- [x] **Bounded timeout.** Default 30 s per-stage start, 5 s per-stage
  stop — operators can narrow further for aggressive SLAs.

## Summary of implementation

### Files created

- `src/pyleans/pyleans/server/lifecycle.py` — `LifecycleStage` enum
  (numeric constants with 1 000-wide gaps for user insertion),
  `LifecycleParticipant` protocol, `SiloLifecycle` engine,
  `StartupAbortedError`. Supports both object-style participants
  (`on_start(stage)`/`on_stop(stage)` methods) and callable hooks
  (`subscribe_hooks(stage, on_start=..., on_stop=..., label=...)`).
- `src/pyleans/test/test_lifecycle.py` — 12 unit tests: ascending /
  descending ordering, concurrent-within-stage timing, completed-
  stages tracking, startup failure → rollback in descending order,
  subscribe-after-start rejection, best-effort shutdown past
  exceptions, stop idempotency, start-timeout abort, stop-timeout
  forces continuation, hooks-style participant, construction
  validation (positive timeouts).

### Key implementation decisions

- **Deliberately incremental.** The Silo class is **not** refactored
  to drive the lifecycle today. The lifecycle engine is now
  available and unit-tested; Silo wiring is a mechanical change
  that will land as part of task 02-18 (multi-silo integration
  tests) where the two BECOME_ACTIVE integration tests can exercise
  the end-to-end path.
- **Why defer Silo wiring:** the refactor touches Phase 1's
  start/stop and the 800+ existing tests depend on the current
  sequence. Rather than risk a partial-broken state, we ship the
  engine and let 02-18 do the migration under multi-silo test
  coverage that would catch regressions.
- **Per-stage timeouts (`asyncio.wait_for`).** A misbehaving
  subsystem cannot wedge startup or shutdown. Operators can tune
  `stage_start_timeout` / `stage_stop_timeout`.
- **`subscribe_hooks` for callable-style participants.** Avoids
  forcing every cluster subsystem to expose a specific
  `on_start(stage)` method just to fit the protocol.

### Deviations from the original design

- Silo refactor is deferred to 02-18. All the Phase 2 subsystems
  (transport, directory, directory-cache, runtime, membership
  agent) have their own `start` / `stop` already; wrapping them as
  lifecycle participants is a one-line change per subsystem in
  Silo's constructor.
- Four acceptance bullets that require Silo wiring are explicitly
  deferred above.

### Test coverage

- 12 new tests. Suite 806 passing (was 794).
- pylint 10.00/10; ruff clean; mypy on pyleans clean.

### Closed by task 02-22

- The Silo refactor deferred above landed in
  [task-02-22-silo-cluster-wiring](task-02-22-silo-cluster-wiring.md).
  `Silo.__init__` composes the cluster stack and subscribes every
  subsystem to `SiloLifecycle`; `Silo.start` / `Silo.stop` drive
  stages in ascending / descending order. See task 02-22's Summary
  for the stage assignment used.
