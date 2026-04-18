# Task 02-18: Multi-Silo Integration Test Suite

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-16-remote-grain-invoke.md](task-02-16-remote-grain-invoke.md)
- [task-02-17-silo-lifecycle-stages.md](task-02-17-silo-lifecycle-stages.md)

## References
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md)
- [plan.md](../plan.md) -- Phase 2 milestone ("Two silo processes on localhost, a grain call from silo A executes on silo B")

## Description

Per-task unit tests prove each subsystem works in isolation. Multi-silo reliability is an emergent property of interactions between subsystems, so it needs its own integration layer. This task ships an **end-to-end harness** that spins up N silos in the same test process (each with its own event loop and port) and a curated scenario catalog that exercises the failure modes Phase 2 is designed to survive.

These tests are slow (seconds, not milliseconds) and run under a distinct pytest marker so the fast suite stays fast.

### Files to create

- `src/pyleans/test/integration/__init__.py`
- `src/pyleans/test/integration/harness.py` -- `ClusterHarness` helper
- `src/pyleans/test/integration/test_cluster_basics.py`
- `src/pyleans/test/integration/test_failure_scenarios.py`
- `src/pyleans/test/integration/test_directory_recovery.py`

### Harness

```python
class ClusterHarness:
    """Spin up N silos on localhost in the same process.

    Each silo runs on its own asyncio task with distinct ports. Shared
    membership uses a tempdir YAML file. Cluster_id is randomized per
    test to prevent cross-test pollution.
    """

    def __init__(self, n: int, *, grains: list[type]) -> None: ...

    async def start(self) -> None: ...

    async def stop_silo(self, index: int, *, graceful: bool = True) -> None:
        """graceful=False simulates a crash (kill the silo task, skip shutdown)."""

    async def start_silo(self, index: int) -> None:
        """Re-start a previously stopped silo. New epoch, same host:port."""

    async def partition(self, a: int, b: int) -> None:
        """Inject a simulated partition between silos a and b by installing
        a transport-level filter that drops frames between them."""

    async def heal_partition(self, a: int, b: int) -> None: ...

    def client(self) -> ClusterClient: ...

    @property
    def silos(self) -> list[Silo]: ...

    async def __aenter__(self) -> "ClusterHarness": ...
    async def __aexit__(self, *exc) -> None: ...
```

Partition injection is done via a test-only hook on `TcpClusterTransport` that filters frames by `(source_silo, target_silo)` pair before handing them to the connection. This avoids needing actual iptables/netns manipulation and keeps tests portable.

### Scenario catalog

Each scenario below becomes a pytest marked `@pytest.mark.integration`.

**test_cluster_basics.py**

1. `test_two_silos_form_cluster` -- start s1, then s2; both end up with `active == {s1, s2}` within 5 s.
2. `test_grain_call_routes_to_owner` -- 2 silos, grain activates on whichever silo the directory picks; calling from the other silo returns the same value regardless of which silo holds the activation.
3. `test_grain_state_persisted_across_silos` -- grain activates on A, writes state, deactivates; next call from B re-activates on the placement-chosen silo with state re-loaded.
4. `test_directory_cache_hit` -- 100 calls to same grain, only the first miss hits the owning silo for a directory lookup.

**test_failure_scenarios.py**

5. `test_silo_crash_detected` -- 3 silos, kill silo 2 (non-graceful); within `~3 * probe_timeout`, remaining silos agree silo 2 is Dead.
6. `test_grain_reactivates_after_host_crash` -- grain active on silo B, kill B; next call from A triggers re-activation on a surviving silo; state reloaded from storage.
7. `test_caller_retries_on_connection_loss` -- grain active on B, kill B mid-call; caller on A catches the connection error, cache invalidates, retry finds the grain on another silo.
8. `test_partition_no_false_death_declaration` -- partition between s1 and s2 while both still reach s3; s3's indirect-probe confirms both are alive; no DEAD declarations.
9. `test_membership_table_unavailable_pauses_voting` -- remove read permission on the YAML file; silos log warnings but do not vote each other dead.
10. `test_oversized_frame_isolates_connection` -- inject an oversized frame on one connection; that connection closes; other connections stay up; other grain calls succeed.

**test_directory_recovery.py**

11. `test_rebuild_after_owner_death` -- 3 silos, silo A owns the arc holding grain G (hosted on silo B); kill A; B still hosts G; lookup from C eventually finds G on B after rebuild.
12. `test_split_view_resolves_to_lower_epoch` -- inject a brief partition causing silos 1 and 2 to activate the same grain; partition heals; rebuild deactivates the higher-epoch activation.
13. `test_graceful_silo_shutdown_hands_off_directory` -- 3 silos, shutdown silo 2 cleanly; its owned directory entries transfer to the new owner; no calls fail during shutdown.

### Determinism aids

Integration tests must not flake on slow CI. Mitigations:

- Use `asyncio.timeout` with generous deadlines (10 s), not tight ones.
- Each assertion that waits for a cluster convergence wraps `asyncio.wait_for` around a polling loop -- never a bare `asyncio.sleep`.
- The failure-detector `FailureDetectorOptions` is tuned aggressively for tests (probe_timeout=0.5 s, probe_timeout_per_attempt=0.2 s, num_missed_probes_limit=2) via a test fixture. This keeps test runtime under 30 s total while still exercising the real code paths.
- Every test calls `await harness.stop()` in `async with` cleanup even on assertion failure. Port leaks between tests break CI; the harness must close every listener.

### What this task does NOT include

- Performance benchmarks (Phase 4 concern).
- Chaos tests beyond the scenarios listed (partition, crash, oversized frame). If a reviewer wants more, they can extend the catalog, but the list above is the minimum coverage Phase 2 must ship with.
- Cross-machine tests. Everything runs on localhost in a single pytest process.

### Acceptance criteria

- [ ] `ClusterHarness` starts N silos in one process, each with distinct ports, shared membership file
- [ ] `stop_silo(index, graceful=False)` simulates a crash without triggering clean shutdown
- [ ] `partition(a, b)` drops frames between the named silos without affecting others
- [ ] All 13 scenarios above pass deterministically (no flakes over 100 runs)
- [ ] `@pytest.mark.integration` marker registered; pytest default selection excludes it; `pytest -m integration` includes it
- [ ] `tox`/CI config documents how to run the integration suite
- [ ] Total integration suite runtime < 60 s on developer hardware

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
