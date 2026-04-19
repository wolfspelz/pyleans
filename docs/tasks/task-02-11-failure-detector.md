# Task 02-11: Failure Detector -- Probe Protocol, Suspicion, Voting, Self-Monitoring

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-02-consistent-hash-ring.md](task-02-02-consistent-hash-ring.md)
- [task-02-08-tcp-cluster-transport.md](task-02-08-tcp-cluster-transport.md)
- [task-02-09-membership-table-extensions.md](task-02-09-membership-table-extensions.md)

## References
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §3.2 protocol overview, §3.4 suspicion and voting, §3.5 indirect probing, §3.6 self-monitoring, §3.7 enforcing perfect detection, §3.10 IAmAlive
- Research: Hashicorp Lifeguard (arxiv.org/abs/1707.00788), SWIM protocol (Das/Gupta/Motivala 2002), Orleans `MembershipAgent` implementation

## Description

This is the heart of Phase 2 -- the protocol that decides which silos are alive. Every component above it (directory, routing, placement) trusts the answer, so the implementation must be both **accurate** (never mistakenly declares a healthy silo dead) and **complete** (eventually declares actually-dead silos dead). Orleans solved this with a combination we replicate:

- **Direct peer-to-peer probes** over the same transport connections used for grain calls -- probes fail when the actual network path is broken, not when an unrelated ZK session expires.
- **Suspicion accumulation** in the membership table with a voting threshold -- no single silo can unilaterally declare another dead.
- **Self-monitoring** -- a stressed silo widens its own probe timeouts so it does not vote healthy peers dead.
- **Indirect probing** -- third-party confirmation before pulling the trigger.

### Files to create

- `src/pyleans/pyleans/cluster/failure_detector.py`
- `src/pyleans/pyleans/cluster/membership_agent.py` -- orchestrator that owns the detector and reconciles state

### Tunables (Orleans-inspired defaults)

```python
@dataclass
class FailureDetectorOptions:
    num_probed_silos: int = 3                # successors on the ring each silo monitors
    probe_timeout: float = 10.0              # interval between probes
    probe_timeout_per_attempt: float = 5.0   # per-probe RPC timeout
    num_missed_probes_limit: int = 3         # miss threshold before voting
    num_votes_for_death_declaration: int = 2 # concurrent voters needed to kill a silo
    death_vote_expiration: float = 180.0     # suspicions older than this are ignored
    indirect_probe_intermediaries: int = 2   # random intermediaries for indirect probe
    i_am_alive_interval: float = 30.0        # silo self-heartbeat cadence
    table_poll_interval: float = 10.0        # periodic full table read
    num_missed_table_i_am_alive_limit: int = 3   # Orleans' IAmAlive staleness threshold for join
```

These defaults match Orleans 9/10. Exposed so production deployments can tune accuracy/completeness per cluster.

### Probe target selection

Every silo, after reading the membership table, builds the consistent hash ring ([task-02-02](task-02-02-consistent-hash-ring.md)) from active peers and calls `ring.successors(local_silo, num_probed_silos)`. Those are the silos it probes. The ring gives us uniform, deterministic, cluster-wide monitoring assignment with no coordination.

Edge cases:
- Active silo count <= `num_probed_silos + 1`: monitor all other active silos.
- Single silo: monitor nothing (the silo is trivially alive).

### Probe RPC

Implemented over the transport's `send_ping`:

```python
async def probe(peer: SiloAddress) -> ProbeResult:
    try:
        rtt = await transport.send_ping(peer, timeout=options.probe_timeout_per_attempt)
        return ProbeResult(ok=True, rtt=rtt)
    except TransportTimeoutError:  return ProbeResult(ok=False, reason="timeout")
    except TransportConnectionError as e:  return ProbeResult(ok=False, reason=str(e))
```

Probes ride the same connection used for grain calls -- a probe that succeeds when grain calls are failing is a bug in the transport layer ([task-02-06](task-02-06-silo-connection.md)), not the detector.

### Suspicion and voting

Per-peer state machine:

```
HEALTHY ---(miss_probe)---> SUSPICIOUS (miss_count++)
SUSPICIOUS -(probe ok)----> HEALTHY (miss_count=0)
SUSPICIOUS -(miss_count >= num_missed_probes_limit)-> VOTING
VOTING  ----(write suspicion to table)----> either OBSERVED_DEAD or back to SUSPICIOUS (ok)
```

On transition to VOTING:

1. Read the peer's row (`read_silo(peer)`) to get fresh etag + existing suspicions.
2. Purge suspicions older than `death_vote_expiration` (Orleans §3.4).
3. If existing_suspicions + 1 >= `num_votes_for_death_declaration`:
   - This probe is the **deciding vote**. Issue an **indirect probe** first (see next section) before pulling the trigger. If confirmed, write both the new suspicion AND `status = DEAD` atomically via `try_update_silo`.
4. Otherwise: `try_add_suspicion(peer, vote, expected_etag)` just appends our suspicion.
5. On `TableStaleError`: re-read and re-evaluate. Some other silo may have already declared the peer dead.
6. On `MembershipUnavailableError`: wait `table_poll_interval` and retry. Do NOT escalate -- Orleans §3.9 (accuracy before completeness).

### Indirect probing (Lifeguard)

Before casting the deciding vote, ask `indirect_probe_intermediaries` randomly chosen other active silos to probe the suspect:

```python
async def indirect_probe(peer: SiloAddress) -> bool:
    intermediaries = random.sample(active_silos - {peer, local_silo}, k)
    results = await asyncio.gather(
        *[transport.send_request(i, INDIRECT_PROBE_REQ, peer) for i in intermediaries],
        return_exceptions=True,
    )
    return any(r.ok for r in results if isinstance(r, IndirectProbeResp))
```

If any intermediary reports success: the peer is alive from at least one perspective -- **abort the vote**, keep the suspicion but do not kill. If all intermediaries also fail or are themselves unreachable: proceed with the death declaration.

Each silo exposes an `INDIRECT_PROBE` message handler that attempts a direct probe and returns the result. Implemented as a dedicated `MessageType.REQUEST` with a distinct header tag so the transport routes it to the detector.

### Self-monitoring

A silo under stress produces false positives (it misses probes because it's too loaded to respond in time, not because its peer is dead). Lifeguard's fix: the silo scores its own health and, when stressed, widens its own probe timeouts.

Health inputs (all cheap to sample):

- Event loop responsiveness: `time.monotonic()` drift between scheduled and actual execution of a probe (threshold: <100 ms delay = good, >1 s = bad).
- Outstanding in-flight requests vs `max_in_flight_requests` -- >80% full = bad.
- Recent probe-send failures originating here.
- Active status in membership table.

Score 0..8 (0 = perfectly healthy). A stressed silo (score > 0) multiplies `probe_timeout_per_attempt` by `(1 + score / 4)`, giving itself more slack and simultaneously making itself more likely to be the first voted dead (because it has worse reachability from others' perspective).

### Self-termination

Once a silo reads the table and sees its own row at `status == DEAD`, it must stop:

1. Log FATAL.
2. Stop accepting new grain calls.
3. Deactivate in-memory grains (best effort).
4. Stop the transport.
5. Exit the process (non-zero exit code so the supervisor restarts with a new epoch).

Why: Orleans §3.7 "enforcing perfect failure detection" -- the rest of the cluster has already decided this silo is gone. The only safe move is to agree. Continuing to operate risks split-brain and double activations.

### Snapshot broadcast

After any mutating write to the membership table, the writing silo broadcasts the new snapshot to every active peer (via `transport.send_one_way(MEMBERSHIP_SNAPSHOT, body)`). Receivers apply it to their cache without re-reading the table. This shortcuts propagation from minutes (worst-case table-poll interval) to milliseconds.

Broadcast is best-effort: if delivery fails, the peer will catch up on its next `table_poll_interval`. Accuracy is not affected.

### IAmAlive

Every `i_am_alive_interval`, the silo writes its own `i_am_alive` timestamp via `try_update_silo`. This is independent of probe-driven heartbeats:

- Diagnostic value -- operators see when a silo was last alive.
- Disaster recovery -- when a new silo joins, it SKIPS connectivity validation against peers whose `i_am_alive` is stale beyond `num_missed_table_i_am_alive_limit * i_am_alive_interval`. This lets a cluster boot when the table still contains rows from crashed silos that never unregistered.

### Orchestration -- `MembershipAgent`

One per silo, started during `BecomeActive` lifecycle stage ([task-02-17](task-02-17-silo-lifecycle-stages.md)):

```python
class MembershipAgent:
    async def start(self) -> None:
        self._probe_task  = asyncio.create_task(self._probe_loop())
        self._poll_task   = asyncio.create_task(self._table_poll_loop())
        self._alive_task  = asyncio.create_task(self._i_am_alive_loop())
        transport.on_connection_lost(self._on_peer_disconnect)

    async def _probe_loop(self): ...
    async def _table_poll_loop(self): ...
    async def _i_am_alive_loop(self): ...

    async def stop(self) -> None:
        """Cancel all three loops, write status=SHUTTING_DOWN, then DEAD."""
```

`_on_peer_disconnect` is a fast-path signal: a lost transport connection is not proof of death (could be local network hiccup) but is a hint to probe that peer more aggressively. Treat it as one "missed probe" on the next scheduled probe cycle.

### Acceptance criteria

- [x] Probe targets computed from hash ring successors; matches across silos given same membership
- [x] Probe success updates `last_activity`, resets `miss_count`
- [x] `num_missed_probes_limit` consecutive misses triggers suspicion write
- [x] Deciding vote performs indirect probe before declaring dead
- [x] Indirect probe success aborts the death vote; suspicion stays
- [x] Indirect probe all-failure proceeds with atomic status=DEAD write
- [x] `TableStaleError` during write triggers re-read and re-evaluate (never loses the change)
- [x] `MembershipUnavailableError` suspends voting (no silo declared dead while table is down)
- [x] Self-monitoring score widens own probe timeouts under stress (unit-tested via `record_loop_drift`; live stall-injection test deferred to 02-18)
- [x] Self-termination on reading own DEAD row: agent calls a clean "self terminate" hook
- [x] Snapshot broadcast after every table write (best-effort, no broadcast failure breaks write)
- [x] IAmAlive written every `i_am_alive_interval` (loop wiring + per-iteration unit tests)
- [ ] Integration test: 3 silos, kill one, remaining two agree on death within `~3 * probe_timeout` seconds -- **deferred to task 02-18** (needs real silo lifecycle from 02-17)
- [ ] Integration test: partitioned table (inject errors) -- no false death declarations -- **deferred to task 02-18**
- [x] Unit tests cover every state transition and every Orleans §3.4 bullet (62 new tests; see Summary below)

## Findings of code review

- [x] **Circular-import hazard at package boundary.** Introducing the failure
  detector into `pyleans.cluster/__init__.py` transitively imported
  `pyleans.transport.cluster`, which in turn had
  `from pyleans.cluster import ClusterId`. That chain triggered
  `ImportError: cannot import name 'IClusterTransport' from partially
  initialized module`. Fixed by changing the four call sites
  (`transport/cluster.py`, `transport/handshake.py`,
  `transport/tcp/transport.py`, `transport/tcp/manager.py`) to import
  `ClusterId` from `pyleans.cluster.identity` directly, eliminating the
  package-init cycle for good. Ordering workarounds in the package
  `__init__` were rejected because ruff's import-sorting would reverse them.
- [x] **Redundant `except CancelledError: raise` bracketing.** Initial agent
  loops wrapped `await` calls with
  `except asyncio.CancelledError: raise; except Exception: log()`.
  Pylint flagged `W0706 (try-except-raise)` because `CancelledError`
  derives from `BaseException` in Python 3.12+ and is not caught by
  `except Exception` anyway. Removed the explicit re-raise arms; left a
  short comment explaining why.
- [x] **Self-monitor score decay.** A silo that was briefly stressed must
  recover when it returns to idle; otherwise its probes permanently run
  at the widened timeout. `record_loop_drift(< 0.1 s)` and
  `reset_probe_send_failure()` both decay by 1, and `TestSelfMonitor`
  asserts the score returns to 0 after enough healthy ticks.
- [x] **Duplicate-vote guard.** Without a guard, a silo whose probes keep
  failing would append a fresh `SuspicionVote` every cycle, inflating
  the row's suspicions list. `_attempt_vote` short-circuits when the
  local silo already has an unexpired vote on the peer. Tested.
- [x] **Expired-vote suppression.** The row's `suspicions` grow
  append-only, but votes older than `death_vote_expiration` must not
  count toward the death-declaration threshold. `_fresh_distinct_voters`
  filters by a `wall_clock() - death_vote_expiration` cutoff. Tested
  with a fast-forward clock in `test_expired_votes_not_counted`.
- [x] **Self-terminate hook safety.** The hook is fired under the probe
  loop's task; a raising hook would take the loop down. Wrapped the
  call in `try/except Exception` so broken hooks only log.

## Findings of security review

- [x] **INDIRECT_PROBE request body validation.** The handler decodes
  `body` as UTF-8 and parses it as `host:port:epoch`. A malformed or
  non-UTF-8 body triggers a FAIL response plus a warning log — the
  process never raises. Covered by
  `test_handler_returns_fail_on_malformed_body`.
- [x] **MEMBERSHIP_SNAPSHOT wire decoding.** `decode_snapshot` wraps
  `json.loads`, schema validation, and per-field coercion behind a
  single `ValueError`. The agent's `message_handler` catches it and
  logs, so a peer cannot crash the local agent by sending junk bytes.
  Covered by three codec tests
  (`test_malformed_bytes_raise_value_error`,
  `test_missing_fields_raise_value_error`,
  `test_non_object_raises_value_error`) plus the agent-level
  `test_malformed_snapshot_does_not_raise`.
- [x] **Stale-snapshot ignoring.** A peer sending an older snapshot
  (`version < local.version`) cannot downgrade the local cache; the
  agent compares versions before applying. Prevents a split-brain peer
  from resurrecting a DEAD row. Covered by
  `test_older_snapshot_is_ignored`.
- [x] **Broadcast best-effort isolation.** `_safe_one_way` catches every
  exception from `transport.send_one_way` so one hostile/broken peer
  cannot break the broadcast loop for all others. Covered by
  `test_broadcast_tolerates_peer_send_failure`.
- [x] **No secret leakage.** Logs include silo-ids, miss counts, and
  suspicion counts — none of which are sensitive. Never logs the
  snapshot body or the peer's etag value.
- [x] **Vote accounting resistant to replay.** Each vote is keyed on
  `suspecting_silo` (silo id) and `timestamp`; duplicate submissions
  from the same silo at the same wall time are deduplicated by set
  construction in `_fresh_distinct_voters`. An attacker injecting
  extra votes would still need a valid etag — the OCC write catches
  divergence.
- [x] **Self-terminate is observable but not destructive.** The agent
  sets `_running = False` and calls the user-supplied hook; it does not
  invoke `sys.exit`. The silo lifecycle owner (task 02-17) is the
  component that actually terminates the process.
- [x] **Deferred integration tests.** "3 silos, kill one, remaining two
  agree on death" and the partitioned-table integration test need a
  working silo lifecycle (task 02-17) and real transport stack — they
  are covered as part of task 02-18 (multi-silo integration tests).
  The core detector logic is fully validated by the 37 unit tests in
  `test_failure_detector.py` and 25 tests in `test_membership_agent.py`.

## Summary of implementation

### Files created

- `src/pyleans/pyleans/cluster/failure_detector.py` (≈410 LOC) —
  `FailureDetectorOptions`, `ProbeResult`, `PeerHealth`, `SelfMonitor`,
  `FailureDetector`. The detector owns per-peer state and the OCC
  write path but does not drive its own event loop. Public entry
  points: `probe_targets`, `probe_once`, `indirect_probe`,
  `pick_intermediaries`, `handle_indirect_probe_request`,
  `on_probe_result`, `bump_miss_count`.
- `src/pyleans/pyleans/cluster/membership_agent.py` (≈430 LOC) —
  `MembershipAgent` composes a `FailureDetector` and drives three
  asyncio tasks: the probe loop, the table-poll loop, and the IAmAlive
  loop. Exposes `message_handler` for the transport to dispatch
  `INDIRECT_PROBE` and `MEMBERSHIP_SNAPSHOT` messages into the agent.
  Also ships `encode_snapshot` / `decode_snapshot` — JSON codec for
  the snapshot-broadcast wire body.
- `src/pyleans/test/test_failure_detector.py` (37 tests) — unit
  coverage for every acceptance criterion touching the detector:
  options validation, self-monitor scoring/decay, target selection,
  miss counting, deciding-vote indirect probe (ok aborts, fail
  proceeds), stale-etag retry, membership-unavailable suspension,
  double-vote suppression, expired-vote suppression.
- `src/pyleans/test/test_membership_agent.py` (25 tests) — snapshot
  codec round-trip, malformed-body handling, `message_handler`
  dispatch, self-terminate via probe cycle and via snapshot broadcast,
  probe-cycle end-to-end with fake transport/membership, i_am_alive
  happy path + missing-row / stale-etag / unavailable-table tolerance,
  disconnect fast-path bumping miss count, broadcast best-effort
  resilience, start/stop/double-start lifecycle.

### Files modified

- `src/pyleans/pyleans/cluster/__init__.py` — re-exports the new
  symbols and reorders imports alphabetically per ruff.
- `src/pyleans/pyleans/transport/cluster.py` — switched `ClusterId`
  import to `pyleans.cluster.identity` to break the circular-import
  cycle introduced by re-exporting the detector from the `cluster`
  package.
- `src/pyleans/pyleans/transport/handshake.py`,
  `src/pyleans/pyleans/transport/tcp/transport.py`,
  `src/pyleans/pyleans/transport/tcp/manager.py` — same
  `ClusterId` import adjustment.

### Key implementation decisions

- **Two-layer split.** `FailureDetector` is the pure-decision layer;
  `MembershipAgent` owns the loops. This keeps every decision
  testable without an asyncio scheduler and confines loop lifecycle
  to a single class.
- **Self-monitoring score model.** Orleans inspired, 0..8 scale, two
  additive contributors (event-loop drift, probe-send failures).
  Adjusted timeout = `base * (1 + score / 4)` — linear widening, max
  3×. Drift and send-failure counters both decay when their inputs
  normalise, so a transient spike does not permanently widen
  timeouts.
- **INDIRECT_PROBE wire shape.** Header tag
  `b"pyleans/indirect-probe/v1"`, body is the suspect's `silo_id` in
  UTF-8, response body is either `b"OK"` or `b"FAIL"`. Kept
  deliberately trivial — this is a health check, not a data channel.
- **Snapshot broadcast uses JSON.** Human-readable, no
  deserialisation pickle risk, round-trips every `SiloInfo` field
  exactly. Header tag `b"pyleans/membership-snapshot/v1"`.
- **Self-termination hook.** The agent sets `_running=False` and
  fires a user-supplied `Callable[[], Awaitable[None]]`. It does not
  call `sys.exit` — the silo runtime (task 02-17) is the layer that
  owns the process lifecycle. This keeps the agent testable without
  patching `sys.exit`.
- **Disconnect fast-path.** `_on_peer_disconnect` bumps the peer's
  `miss_count` by 1 (capped at the limit). Matches the task spec
  "Treat it as one 'missed probe' on the next scheduled probe cycle."
  No vote is cast directly — voting still requires the probe loop to
  confirm on the next cycle.
- **TableStaleError retry.** `_vote_or_declare` re-reads the row and
  re-evaluates up to `_MAX_STALE_RETRY=3` times. On
  `MembershipUnavailableError` it bails out immediately (accuracy
  before completeness — Orleans §3.9).

### Deviations from the original design

- The integration tests listed in the acceptance criteria
  ("3 silos, kill one", "partitioned table no false positives") are
  not in this task. They require a working silo lifecycle
  (task 02-17) and live multi-silo transport, and are covered by
  task 02-18. The detector's decision logic is exhaustively
  unit-tested (62 tests total).
- The agent's `message_handler` only routes `INDIRECT_PROBE` and
  `MEMBERSHIP_SNAPSHOT`. Routing grain calls through the same
  transport handler is task 02-16 (remote grain invocation).
- `FailureDetectorOptions.num_missed_table_i_am_alive_limit` is
  defined for future use by task 02-17's silo-join connectivity
  validation; the detector itself does not consult it today.

### Test coverage

- 37 failure-detector unit tests
  (`src/pyleans/test/test_failure_detector.py`) + 25
  membership-agent unit tests
  (`src/pyleans/test/test_membership_agent.py`) = 62 new tests.
- Every acceptance criterion marked as unit-testable has a dedicated
  test; the ones requiring a real silo stack are deferred to 02-18
  (documented above).
- Full suite: 718 passing (up from 656 at task start).
- `ruff check` / `ruff format --check` / `pylint` (10.00/10) / `mypy`
  (no new errors; 24 pre-existing errors in unrelated files remain
  unchanged).
