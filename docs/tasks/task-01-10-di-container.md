# Task 01-10: Dependency Injection Container

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-08-grain-runtime.md](task-01-08-grain-runtime.md)
- [task-01-09-grain-reference.md](task-01-09-grain-reference.md)
- [task-01-05-provider-abcs.md](task-01-05-provider-abcs.md)

## References
- [adr-dependency-injection](../adr/adr-dependency-injection.md)
- [orleans-advanced.md](../orleans-architecture/orleans-advanced.md) -- Section 8 (Dependency Injection)

## Description

Set up the `injector` (type-hint-based) container that provides framework services
to grains via constructor injection. Matching Orleans' all-DI pattern.

### Files to create
- `src/pyleans/pyleans/server/container.py`

### Framework Module

The pyleans framework provides a `PyleansModule` (an `injector.Module` subclass)
that binds framework singletons. The Silo constructs and passes the runtime
instances into the module at startup.

```python
from injector import Injector, Module, provider, singleton

class PyleansModule(Module):
    """Injector module that binds framework services."""

    def __init__(
        self,
        runtime: GrainRuntime,
        grain_factory: GrainFactory,
        timer_registry: TimerRegistry,
        silo_management: SiloManagement,
        stream_manager: StreamManager | None = None,
    ) -> None:
        self._runtime = runtime
        self._grain_factory = grain_factory
        self._timer_registry = timer_registry
        self._silo_management = silo_management
        self._stream_manager = stream_manager

    @provider
    @singleton
    def provide_runtime(self) -> GrainRuntime:
        return self._runtime

    # … analogous @provider @singleton methods for the other services
```

A `create_injector(...)` helper builds the `Injector` and installs `PyleansModule`.

### Grain instantiation with DI

When the runtime activates a grain, it resolves the instance via the
configured `Injector`:

```python
# In GrainRuntime.activate_grain():
grain_class = get_grain_class(grain_id.grain_type)
instance = injector.get(grain_class)   # type-hint args resolved from the module
instance.identity = grain_id
# ... load state, call on_activate
```

### User extension

Users add their own services by writing a second `Module` and composing it
with `PyleansModule` in the `Injector`:

```python
from injector import Module, provider, singleton

class AppModule(Module):
    @provider
    @singleton
    def provide_email(self) -> IEmailService:
        return SmtpEmailService(host="smtp.example.com")

injector = Injector([PyleansModule(...), AppModule()])
```

### Acceptance criteria

- [x] `PyleansModule` binds GrainFactory, TimerRegistry, SiloManagement, StreamManager as singletons
- [x] Grain `__init__` with type-hint parameters receives framework services via `injector.get()`
- [x] Silo builds the `Injector` during startup
- [x] User services injectable alongside framework services (additional `Module`s)
- [x] Unit test: grain constructed with injected services via DI

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Files created
- `src/pyleans/pyleans/server/container.py` — PyleansModule with framework services
- `src/pyleans/test/test_container.py` — 10 tests

### Key decisions
- Container provides JsonSerializer and empty storage_providers as defaults.
- Runtime, GrainFactory, TimerRegistry are Singletons sharing the same runtime instance.
- Logger name configurable via `config.logger_name`.
- Users can extend PyleansModule or override individual providers.

### Deviations
- StreamManager was initially deferred; now added to the container (resolved).
- DI wiring now fully implemented: Silo calls DI container setup, grains use type-hint constructor injection for singleton services (resolved).

### Test coverage
- 10 tests: container creation, all providers resolve, singleton behavior, shared runtime instance, extension via subclass, provider override.