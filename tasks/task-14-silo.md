# Task 14: Silo (Main Entry Point)

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-07-grain-runtime.md](task-07-grain-runtime.md)
- [task-08-grain-reference.md](task-08-grain-reference.md)
- [task-09-di-container.md](task-09-di-container.md)
- [task-10-file-storage.md](task-10-file-storage.md)
- [task-11-yaml-membership.md](task-11-yaml-membership.md)
- [task-12-grain-timers.md](task-12-grain-timers.md)
- [task-13-in-memory-streaming.md](task-13-in-memory-streaming.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Phase 1, Decision 8 (dev mode), Decision 9 (library)
- [orleans-cluster.md](../docs/orleans-cluster.md) -- Silo lifecycle

## Description

Implement the `Silo` class -- the main entry point that wires everything together.
This is the class users create in their `main.py`.

### Files to create
- `pyleans/pyleans/server/silo.py`
- `pyleans/pyleans/server/__init__.py` (re-exports Silo)
- Update `pyleans/pyleans/__init__.py` (re-exports @grain, GrainRef, etc.)

### Design

```python
class Silo:
    """
    A pyleans silo -- hosts grain activations and manages the runtime.

    Usage:
        silo = Silo(
            grains=[CounterGrain, PlayerGrain],
            storage_providers={"default": FileStorageProvider("./data")},
            membership_provider=YamlMembershipProvider("./membership.yaml"),
            stream_providers={"default": InMemoryStreamProvider()},
        )
        await silo.start()
    """

    def __init__(
        self,
        grains: list[type],
        storage_providers: dict[str, StorageProvider] | None = None,
        membership_provider: MembershipProvider | None = None,
        stream_providers: dict[str, StreamProvider] | None = None,
        port: int = 11111,
        gateway_port: int = 30000,
        host: str = "localhost",
        idle_timeout: float = 900.0,
    ):
        """
        Args:
            grains: List of grain classes decorated with @grain.
            storage_providers: Named storage providers. Default: FileStorageProvider.
            membership_provider: Membership provider. Default: YamlMembershipProvider.
            stream_providers: Named stream providers. Default: InMemoryStreamProvider.
            port: Silo port (for future TCP mesh).
            gateway_port: TCP gateway port for client connections. Default: 30000.
            host: Silo host address.
            idle_timeout: Seconds before idle grains are deactivated.
        """

    async def start(self) -> None:
        """
        Start the silo:
        1. Register grain classes
        2. Initialize providers
        3. Set up DI container and wire grain modules
        4. Register in membership table
        5. Start idle collector
        6. Block until stopped
        """

    async def stop(self) -> None:
        """
        Graceful shutdown:
        1. Stop accepting new calls
        2. Deactivate all grains (calls on_deactivate, saves state)
        3. Unregister from membership
        4. Cancel background tasks
        """

    @property
    def grain_factory(self) -> GrainFactory:
        """Access to grain factory (for co-hosted clients like FastAPI)."""

    @property
    def runtime(self) -> GrainRuntime:
        """Access to the grain runtime."""
```

### Dev mode defaults

When no providers are specified, use sensible defaults:
- `storage_providers={"default": FileStorageProvider("./pyleans-data/storage")}`
- `membership_provider=YamlMembershipProvider("./pyleans-data/membership.yaml")`
- `stream_providers={"default": InMemoryStreamProvider()}`

### Lifecycle

```
start()
  |-> Register grain classes in registry
  |-> Initialize storage/membership/stream providers
  |-> Create GrainRuntime
  |-> Create DI container (PyleansContainer or user-supplied)
  |-> Wire DI into grain modules
  |-> Create GrainFactory, TimerRegistry, StreamManager
  |-> Register silo in membership table
  |-> Start idle collection background task
  |-> Start heartbeat background task
  |-> Log "Silo started on {host}:{port}"
  |-> await stop_event.wait()

stop()
  |-> Update membership status -> shutting_down
  |-> Deactivate all grains
  |-> Cancel timers
  |-> Unregister from membership
  |-> Log "Silo stopped"
  |-> Set stop_event
```

### Signal handling

Register SIGINT/SIGTERM handlers to trigger graceful `stop()`.

### Acceptance criteria

- [x] `Silo(grains=[...])` with defaults starts successfully
- [x] Grain calls work via `silo.grain_factory.get_grain()`
- [x] Graceful shutdown deactivates grains and saves state
- [x] Silo registers/unregisters in membership table
- [x] Heartbeat updates membership periodically
- [x] Ctrl+C triggers graceful shutdown
- [x] Integration test: start silo, call grain, stop silo, verify state persisted

## Findings of code review

No issues found. Review checked:
- Clean code: Silo has single responsibility (wiring components together). No logic duplication with runtime.
- SOLID: All providers injected via constructor. Abstractions (ABCs) used for storage, membership, streaming.
- Type hints: All public APIs fully typed. mypy strict mode passes.
- No dead code, magic constants, or unused imports.
- Tests cover all acceptance criteria including integration end-to-end.

## Findings of security review

No issues found. Review checked:
- No new system boundary inputs exposed (host/port stored for future TCP mesh, not bound).
- Signal handlers: wrapped in try/except NotImplementedError for Windows ProactorEventLoop compatibility.
- No path traversal, injection, or deserialization risks introduced.
- No unbounded resource consumption — heartbeat task properly cancelled on stop, all grains deactivated.
- Shutdown task reference stored to prevent GC collection (RUF006 fix).

## Summary of implementation

### Files created/modified
- **Created**: `pyleans/pyleans/server/silo.py` — Silo class with start/stop lifecycle, signal handling, heartbeat
- **Modified**: `pyleans/pyleans/server/__init__.py` — re-exports Silo
- **Modified**: `pyleans/pyleans/__init__.py` — re-exports grain, GrainId, GrainRef, GrainFactory
- **Created**: `pyleans/test/test_silo.py` — 25 tests across 9 test classes

### Key implementation decisions
- Added `start_background()` method for non-blocking start (FastAPI embedding, tests) alongside blocking `start()`.
- Silo directly creates GrainRuntime, GrainFactory, TimerRegistry rather than using PyleansContainer — simpler wiring, DI container available as extension point for user services.
- Signal handlers use try/except for Windows compatibility (ProactorEventLoop doesn't support add_signal_handler).
- Shutdown task stored as `self._shutdown_task` to prevent garbage collection (addresses RUF006 lint rule).

### Deviations from original design
- Removed `container` parameter from constructor. The DI container (`PyleansContainer`) is available as a separate utility for users who want it, but Silo handles wiring directly for simplicity.
- Added `start_background()` method not in original spec — needed for practical embedding and testing.

### Test coverage summary
- 25 tests covering: start/stop lifecycle, blocking start, double-stop safety, properties, grain registration, stateless and stateful grain calls, lifecycle hooks, multiple independent grains, membership registration/unregistration, shutdown status transition, heartbeat periodic execution, heartbeat cancellation, graceful shutdown with grain deactivation, state persistence through stop, full integration restart test, concurrent grain calls, default providers, custom providers, package imports.