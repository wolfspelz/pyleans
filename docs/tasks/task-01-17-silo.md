# Task 01-17: Silo (Main Entry Point)

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-08-grain-runtime.md](task-01-08-grain-runtime.md)
- [task-01-09-grain-reference.md](task-01-09-grain-reference.md)
- [task-01-10-di-container.md](task-01-10-di-container.md)
- [task-01-11-file-storage.md](task-01-11-file-storage.md)
- [task-01-12-yaml-membership.md](task-01-12-yaml-membership.md)
- [task-01-13-grain-timers.md](task-01-13-grain-timers.md)
- [task-01-14-in-memory-streaming.md](task-01-14-in-memory-streaming.md)
- [task-01-15-network-port.md](task-01-15-network-port.md) -- Silo owns the first networking component in Phase 1 (the gateway listener); the network port is what keeps that testable
- [task-01-16-in-memory-network-simulator.md](task-01-16-in-memory-network-simulator.md) -- Silo tests use the simulator

## References
- [adr-dev-mode](../adr/adr-dev-mode.md), [adr-library-vs-cli](../adr/adr-library-vs-cli.md)
- [plan.md](../plan.md) -- Phase 1
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- Silo lifecycle

## Description

Implement the `Silo` class -- the main entry point that wires everything together.
This is the class users create in their `main.py`.

### Files to create
- `src/pyleans/pyleans/server/silo.py`
- `src/pyleans/pyleans/server/__init__.py` (re-exports Silo)
- Update `src/pyleans/pyleans/__init__.py` (re-exports @grain, GrainRef, etc.)

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
        network: INetwork | None = None,
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
            network: Pluggable network adapter. Default: AsyncioNetwork (production
                TCP). Tests pass InMemoryNetwork so no OS ports are bound.
                See task-01-15 and adr-network-port-for-testability.
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

### SiloManagement — framework service for silo metadata

The pyleans library provides a `SiloManagement` service that exposes silo
metadata to grains via true dependency injection (type-hint constructor injection).
Grains that need silo info declare it in their constructor — the DI container
resolves it automatically.

#### Files to create
- `src/pyleans/pyleans/server/silo_management.py`
- `src/pyleans/pyleans/server/grains.py` (exports `system_grains()`)

```python
# src/pyleans/pyleans/server/silo_management.py

class SiloManagement:
    """Service providing silo metadata to grains.

    Bound to grain instances as ``self.silo_management`` by the runtime.
    """

    def get_info(self) -> dict[str, object]:
        """Return a dictionary of silo properties.

        Keys:
            silo_id: str           — encoded silo address (host_port_epoch)
            host: str              — silo host address
            hostname: str          — OS hostname
            platform: str          — OS platform (e.g. "Windows", "Linux")
            port: int              — silo port (for future cluster mesh)
            gateway_port: int      — TCP gateway port for client connections
            epoch: int             — silo start epoch
            status: str            — current silo status (e.g. "active")
            uptime_seconds: float  — seconds since silo start
            grain_count: int       — number of currently active grains
            idle_timeout: float    — grain idle timeout in seconds
        """
```

#### Integration with the Silo

The Silo creates a `SiloManagement` instance and registers it in the
the DI container. During silo startup, DI container setup enables
type-hint constructor injection across grain modules. Grains receive
`SiloManagement` (and other services) via constructor injection — the
runtime does NOT bind it as an attribute.

```python
# In Silo.__init__() or start():
# DI container created by Silo
# services bound via injector
self._# DI resolved via injector  # enables  in grain modules

# In grain code:
@grain(state_type=CounterState, storage="default")
class CounterGrain:
    
    def __init__(self, silo_mgmt: SiloManagement ):
        self._silo_mgmt = silo_mgmt
```

#### system_grains() helper

```python
# src/pyleans/pyleans/server/grains.py
from pyleans.server.string_cache_grain import StringCacheGrain

def system_grains() -> list[type]:
    """Return the list of framework-provided grains.

    Use this in silo configuration to include pyleans system grains:

        silo = Silo(
            grains=[CounterGrain, *system_grains()],
            ...
        )

    Currently includes:
    - StringCacheGrain — simple string key-value store with persistence
    """
    return [StringCacheGrain]
```

#### Grain usage

Grains that need silo info declare it via constructor injection:

```python
from pyleans.server.silo_management import SiloManagement

@grain(state_type=CounterState, storage="default")
class CounterGrain:
    
    def __init__(self, silo_mgmt: SiloManagement ):
        self._silo_mgmt = silo_mgmt

    async def get_silo_info(self) -> dict[str, object]:
        return self._silo_mgmt.get_info()
```

Grains that don't need services keep a plain `__init__` (or none at all).
Only per-grain-instance data (`identity`, `state`, `write_state`, `clear_state`)
is bound by the runtime — all singleton services come through DI.

#### Acceptance criteria (SiloManagement)

- [x] `SiloManagement.get_info()` returns all documented keys
- [x] `grain_count` reflects the current number of active grains
- [x] `uptime_seconds` increases over time
- [x] `SiloManagement` injected into grains via type-hint constructor injection (not runtime-bound)
- [x] Silo calls DI container setup during startup
- [x] `system_grains()` returns a list (currently empty, extensible)
- [x] Unit tests for DI-injected SiloManagement in grains

### StringCacheGrain — framework-provided key-value cache

A built-in grain that implements a simple string key-value store with
persistence. Provided by `system_grains()` so users can opt in.
Each grain instance is identified by key and stores a single string value.

#### Files to create
- `src/pyleans/pyleans/server/string_cache_grain.py`

```python
from dataclasses import dataclass
from pyleans import grain

@dataclass
class StringCacheState:
    value: str = ""

@grain(state_type=StringCacheState, storage="default")
class StringCacheGrain:
    """Simple string key-value store grain.

    Each grain instance (identified by key) holds one string value.
    State is persisted — survives silo restarts. No idle timeout —
    the grain stays in memory until explicitly deactivated.

    Usage from a client:
        cache = client.get_grain(StringCacheGrain, "my-key")
        await cache.set("hello world")
        value = await cache.get()       # "hello world"
        await cache.delete()            # clears persisted state
        await cache.deactivate()        # removes from memory
    """

    async def set(self, value: str) -> None:
        """Set the cached value and persist."""
        self.state.value = value
        await self.write_state()

    async def get(self) -> str:
        """Return the cached value (empty string if never set)."""
        return self.state.value

    async def delete(self) -> None:
        """Clear the persisted state (resets value to empty string)."""
        await self.clear_state()

    async def deactivate(self) -> None:
        """Remove this grain from memory.

        The next call to this grain will re-activate it from persistence.
        Useful for forcing a reload from storage.
        """
        # The runtime handles deactivation when this method completes.
        # Implementation: the grain signals the runtime to deactivate
        # itself after this call returns.
```

#### Integration with system_grains()

`system_grains()` returns `[StringCacheGrain]` so users opt in:

```python
from pyleans.server.grains import system_grains

silo = Silo(
    grains=[CounterGrain, *system_grains()],
    ...
)
```

Users can also import `StringCacheGrain` directly.

#### Deactivation mechanism

The `deactivate()` method needs the runtime to deactivate the grain after
the method returns. Options:
1. The grain calls `self.deactivate_on_idle()` (a runtime-bound method,
   like `write_state`), which schedules deactivation after the current turn.
2. The runtime checks a flag on the activation after each method call.

Option 1 is cleaner and matches Orleans' `DeactivateOnIdle()`.

#### Acceptance criteria (StringCacheGrain)

- [x] `system_grains()` returns a list containing `StringCacheGrain`
- [x] `set("value")` persists the string
- [x] `get()` returns the persisted value
- [x] `get()` returns empty string when never set
- [x] `delete()` clears persisted state
- [x] `deactivate()` removes grain from memory
- [x] Next `get()` after `deactivate()` re-activates from persistence
- [x] State survives silo restart
- [x] Unit tests for all operations and the activate-from-persistence flow

### Dev mode defaults

When no providers are specified, use sensible defaults:
- `storage_providers={"default": FileStorageProvider("./data/storage")}`
- `membership_provider=YamlMembershipProvider("./data/membership.yaml")`
- `stream_providers={"default": InMemoryStreamProvider()}`
- `network=AsyncioNetwork()` (production default — binds real TCP on `gateway_port`).

The GatewayListener is constructed with `network=self._network`, so test suites that construct the Silo with `network=InMemoryNetwork()` never bind a real port.

### Phase 2 extension point (forward reference)

Phase 1's `Silo` is a single-silo, in-process runtime per
[adr-dev-mode](../adr/adr-dev-mode.md). The single-activation contract from
[adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) is
trivially satisfied because there is only one silo. Phase 2 preserves the
contract across multiple silos by wiring the cluster subsystem into the same
`Silo` class:

- Cluster transport ([task-02-08-tcp-cluster-transport.md](task-02-08-tcp-cluster-transport.md))
  — constructed and started after the membership registration stage.
- Distributed grain directory
  ([task-02-13-distributed-grain-directory.md](task-02-13-distributed-grain-directory.md))
  and directory cache ([task-02-14-directory-cache.md](task-02-14-directory-cache.md))
  — passed to `GrainRuntime` so the routing hook from
  [task-02-16-remote-grain-invoke.md](task-02-16-remote-grain-invoke.md) is active.
- Lifecycle stages ([task-02-17-silo-lifecycle-stages.md](task-02-17-silo-lifecycle-stages.md))
  — replace today's ad-hoc start/stop with explicit ordered stages so calls
  are not accepted before the cluster join and directory priming complete.

Phase 1 does not anticipate the full shape of these parameters; Phase 2 will
extend the constructor signature as needed. What Phase 1 commits to is:
`Silo`'s public surface (`grain_factory`, `runtime`, `start`, `stop`,
`start_background`) is stable, and Phase 2 is additive with respect to it.

### Lifecycle

```
start()
  |-> Register grain classes in registry
  |-> Initialize storage/membership/stream providers
  |-> Create GrainRuntime
  |-> Create DI container (injector-based)
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
- [x] `Silo(network=InMemoryNetwork())` binds no OS ports; `silo.gateway_port` reports the simulator's virtual port; grain calls still work end-to-end
- [x] All Silo unit tests use `InMemoryNetwork` via the shared `network` pytest fixture from [task-01-16](task-01-16-in-memory-network-simulator.md)

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
- **Created**: `src/pyleans/pyleans/server/silo.py` — Silo class with start/stop lifecycle, signal handling, heartbeat
- **Modified**: `src/pyleans/pyleans/server/__init__.py` — re-exports Silo
- **Modified**: `src/pyleans/pyleans/__init__.py` — re-exports grain, GrainId, GrainRef, GrainFactory
- **Created**: `src/pyleans/test/test_silo.py` — 25 tests across 9 test classes

### Key implementation decisions
- Added `start_background()` method for non-blocking start (FastAPI embedding, tests) alongside blocking `start()`.
- Silo directly creates GrainRuntime, GrainFactory, TimerRegistry rather than using the DI container — simpler wiring, DI container available as extension point for user services.
- Signal handlers use try/except for Windows compatibility (ProactorEventLoop doesn't support add_signal_handler).
- Shutdown task stored as `self._shutdown_task` to prevent garbage collection (addresses RUF006 lint rule).

### Deviations from original design
- Removed `container` parameter from constructor. The DI container (the DI container) is available as a separate utility for users who want it, but Silo handles wiring directly for simplicity.
- Added `start_background()` method not in original spec — needed for practical embedding and testing.

### Phase 2 forward reference
Phase 2 will extend `Silo` to wire the cluster subsystem (cluster transport,
grain directory, directory cache, lifecycle stages) per
[adr-single-activation-cluster](../adr/adr-single-activation-cluster.md). The
routing hook added to `GrainRuntime.invoke()` by
[task-02-16-remote-grain-invoke.md](task-02-16-remote-grain-invoke.md) is the
only place Phase 1 semantics can change; the gateway listener is already
silo-transparent because it dispatches every request to `runtime.invoke()`, so
no gateway change is required. The stable Phase 1 public surface
(`grain_factory`, `runtime`, `start`, `stop`, `start_background`) is preserved.

### Test coverage summary
- 25 tests covering: start/stop lifecycle, blocking start, double-stop safety, properties, grain registration, stateless and stateful grain calls, lifecycle hooks, multiple independent grains, membership registration/unregistration, shutdown status transition, heartbeat periodic execution, heartbeat cancellation, graceful shutdown with grain deactivation, state persistence through stop, full integration restart test, concurrent grain calls, default providers, custom providers, package imports.