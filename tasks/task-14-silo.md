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
- `src/pyleans/server/silo.py`
- `src/pyleans/server/__init__.py` (re-exports Silo)
- Update `src/pyleans/__init__.py` (re-exports @grain, GrainRef, etc.)

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
        container: containers.DeclarativeContainer | None = None,
        port: int = 11111,
        host: str = "localhost",
        idle_timeout: float = 900.0,
    ):
        """
        Args:
            grains: List of grain classes decorated with @grain.
            storage_providers: Named storage providers. Default: FileStorageProvider.
            membership_provider: Membership provider. Default: YamlMembershipProvider.
            stream_providers: Named stream providers. Default: InMemoryStreamProvider.
            container: DI container. Default: PyleansContainer.
            port: Silo port (for future TCP mesh).
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

- [ ] `Silo(grains=[...])` with defaults starts successfully
- [ ] Grain calls work via `silo.grain_factory.get_grain()`
- [ ] Graceful shutdown deactivates grains and saves state
- [ ] Silo registers/unregisters in membership table
- [ ] Heartbeat updates membership periodically
- [ ] Ctrl+C triggers graceful shutdown
- [ ] Integration test: start silo, call grain, stop silo, verify state persisted

## Summary of implementation
_To be filled when task is complete._