---
status: accepted
date: 2026-04-18
tags: [api-surface, grain, proxy]
---

# Grain Interfaces via `@grain` Decorator

## Context and Problem Statement

Orleans (C#) requires a separate `IGrainInterface` and a `GrainClass`. Porting that pattern to Python would mean duplicate declarations for every grain type. We need a way to define the callable surface of a grain that is:

- minimal and Pythonic (no boilerplate interface classes);
- usable at runtime to generate client-side proxies;
- introspectable — the runtime must know which methods are callable on a grain reference.

## Decision

The `@grain` decorator on a class is the sole interface definition. The decorator registers the class and its public async methods as the grain interface. Proxy objects use `__getattr__` to forward calls. No separate interface class is needed.

```python
@grain
class PlayerGrain(Grain[PlayerState]):
    async def get_name(self) -> str:
        return self.state.name

    async def set_name(self, name: str) -> None:
        self.state.name = name
        await self.write_state()
```

## Considered Options

- **`@grain` decorator + `__getattr__` proxy** — chosen. Minimal, Pythonic, no duplication.
- **ABC-based interfaces** (`class IPlayerGrain(GrainInterface)`) — explicit contracts and typed proxies, but doubles the boilerplate. Rejected.
- **Protocol-based structural typing** — Pythonic for type checking, but not usable for runtime proxy generation. Rejected.

Either rejected option can be added later as an optional layer if typed proxies become a requirement.

## Consequences

- Grain authors write one class — no interface boilerplate.
- Proxies are dynamic; static type checkers cannot fully verify client-side grain calls. Method lookup is resolved at runtime via the decorator registry.
- Adding typed proxies later is possible without breaking existing grains (additive).

## Related

- [adr-grain-base-class](adr-grain-base-class.md)
- [adr-state-declaration](adr-state-declaration.md)
