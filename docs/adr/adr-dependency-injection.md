---
status: accepted
date: 2026-04-18
tags: [di, runtime, grain-construction]
---

# Dependency Injection via `injector` (Guice-style)

## Context and Problem Statement

Grains need framework services (`GrainFactory`, `TimerRegistry`, `SiloManagement`, `StreamManager`) and potentially user-provided services (e.g. `IEmailService`). We want:

- no decorator/marker boilerplate on every grain constructor;
- type-hint-driven resolution (PEP 484 annotations should be enough);
- trivial mocking in unit tests.

## Decision

All services — framework and user — are constructor-injected via the `injector` package (Python port of Google Guice). Grains declare dependencies as plain `__init__` type hints. No decorators, no markers, no `Provide[...]`. The `@grain` decorator automatically marks `__init__` for injection.

```python
@grain(storage="default")
class PlayerGrain(Grain[PlayerState]):
    def __init__(self, grain_factory: GrainFactory, email: IEmailService) -> None:
        self._grain_factory = grain_factory
        self._email = email

    async def send_welcome(self) -> None:
        await self._email.send(self.state.email, "Welcome!", f"Hi {self.state.name}")

    async def join_room(self, room_key: str) -> None:
        room = self._grain_factory.get_grain(ChatRoomGrain, room_key)
        await room.add_member(self.identity.key)
```

The Silo creates an `Injector` with all framework services bound as singletons. The runtime calls `injector.get(grain_class)` to create grain instances with dependencies resolved.

**Framework services available for injection**:

| Service | Purpose |
|---|---|
| `GrainFactory` | Get references to other grains |
| `TimerRegistry` | Register grain timers |
| `SiloManagement` | Silo metadata |
| `StreamManager` | Stream pub/sub |

Grain **identity, state, and logger** are provided by the `Grain[TState]` base class — not DI. See [adr-grain-base-class](adr-grain-base-class.md).

**Testing**: pass mocks directly to the constructor — no container needed.

```python
grain = PlayerGrain(grain_factory=mock_factory, email=MockEmailService())
```

## Considered Options

- **`injector` package** — chosen. Resolves purely from type hints, Guice-style, zero boilerplate on grain constructors.
- **`dependency-injector` package** — requires `@inject` decorator and `Provide[Container.service]` markers on every constructor parameter. Rejected for noise.
- **Manual wiring** — the number of framework services plus user services makes manual construction impractical. Rejected.

## Consequences

- Grain authors write natural Python constructors; DI is invisible unless they need a service.
- `injector` becomes a required runtime dependency of `pyleans.server`.
- Cyclic grain dependencies are resolved at call time via `GrainFactory`, not at construction.

## Related

- [adr-grain-base-class](adr-grain-base-class.md)
- [adr-provider-interfaces](adr-provider-interfaces.md)
