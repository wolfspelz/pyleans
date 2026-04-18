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

- [ ] Probe targets computed from hash ring successors; matches across silos given same membership
- [ ] Probe success updates `last_activity`, resets `miss_count`
- [ ] `num_missed_probes_limit` consecutive misses triggers suspicion write
- [ ] Deciding vote performs indirect probe before declaring dead
- [ ] Indirect probe success aborts the death vote; suspicion stays
- [ ] Indirect probe all-failure proceeds with atomic status=DEAD write
- [ ] `TableStaleError` during write triggers re-read and re-evaluate (never loses the change)
- [ ] `MembershipUnavailableError` suspends voting (no silo declared dead while table is down)
- [ ] Self-monitoring score widens own probe timeouts under stress (test with an event-loop stall injection)
- [ ] Self-termination on reading own DEAD row: agent calls a clean "self terminate" hook
- [ ] Snapshot broadcast after every table write (best-effort, no broadcast failure breaks write)
- [ ] IAmAlive written every `i_am_alive_interval`
- [ ] Integration test: 3 silos, kill one, remaining two agree on death within `~3 * probe_timeout` seconds
- [ ] Integration test: partitioned table (inject errors) -- no false death declarations
- [ ] Unit tests cover every state transition and every Orleans §3.4 bullet

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
