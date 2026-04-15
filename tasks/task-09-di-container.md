# Task 09: Dependency Injection Container

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-07-grain-runtime.md](task-07-grain-runtime.md)
- [task-08-grain-reference.md](task-08-grain-reference.md)
- [task-05-provider-abcs.md](task-05-provider-abcs.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Decision 4 (DI via dependency-injector)
- [orleans-advanced.md](../docs/orleans-advanced.md) -- Section 8 (Dependency Injection)

## Description

Set up the `dependency-injector` container that provides framework services
to grains via constructor injection. Matching Orleans' all-DI pattern.

### Files to create
- `src/pyleans/server/container.py`

### Framework Container

The pyleans framework provides a base container with framework services.
Users extend it or compose with their own container for app services.

```python
class PyleansContainer(containers.DeclarativeContainer):
    """Base container with framework services."""

    config = providers.Configuration()

    # Core runtime (set during silo startup)
    runtime = providers.Singleton(GrainRuntime)
    grain_factory = providers.Singleton(GrainFactory, runtime=runtime)
    timer_registry = providers.Singleton(TimerRegistry, runtime=runtime)
    stream_manager = providers.Singleton(StreamManager)
    logger = providers.Singleton(logging.getLogger, config.logger_name)
```

### Grain instantiation with DI

When the runtime activates a grain, it creates the instance through
`dependency-injector`'s wiring:

```python
# In GrainRuntime.activate_grain():
grain_class = get_grain_class(grain_id.grain_type)
instance = grain_class()  # @inject on __init__ resolves Provide[...] defaults
instance.identity = grain_id
# ... load state, call on_activate
```

The `container.wire(modules=[...])` call during silo startup enables
`@inject` + `Provide[...]` to work across the user's grain modules.

### User extension

```python
# User's app container
class AppContainer(PyleansContainer):
    email_service = providers.Singleton(SmtpEmailService,
        smtp_host=PyleansContainer.config.smtp_host)
```

Or compose:
```python
class AppContainer(containers.DeclarativeContainer):
    pyleans = providers.Container(PyleansContainer)
    email_service = providers.Singleton(SmtpEmailService)
```

### Acceptance criteria

- [ ] `PyleansContainer` provides GrainFactory, TimerRegistry, StreamManager, Logger
- [ ] Grain `__init__` with `@inject` + `Provide[...]` receives framework services
- [ ] User services injectable alongside framework services
- [ ] Container wiring works across grain modules
- [ ] Unit test: grain constructed with injected mock services

## Summary of implementation

### Files created
- `pyleans/pyleans/server/container.py` — PyleansContainer with framework services
- `pyleans/test/test_container.py` — 10 tests

### Key decisions
- Container provides JsonSerializer and empty storage_providers as defaults.
- Runtime, GrainFactory, TimerRegistry are Singletons sharing the same runtime instance.
- Logger name configurable via `config.logger_name`.
- Users can extend PyleansContainer or override individual providers.

### Deviations
- StreamManager not included yet (deferred to Task 13 when it's implemented).

### Test coverage
- 10 tests: container creation, all providers resolve, singleton behavior, shared runtime instance, extension via subclass, provider override.