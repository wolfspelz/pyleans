---
status: accepted
date: 2026-04-18
tags: [grain, state, decorator]
---

# State Declaration via `@grain` Decorator

## Context and Problem Statement

Each grain class must declare its state type and the backing storage provider. Orleans offers two patterns:

- **Legacy**: class-level `[StorageProvider(ProviderName = "store1")]`.
- **Modern (Orleans 7+)**: constructor-parameter `[PersistentState("name", "store")]`, which allows multiple named state objects per grain, each backed by a different provider.

We need one that fits Python's decorator idioms and PoC scope.

## Decision

Grain state is declared in the decorator:

```python
@grain(state_type=PlayerState, storage="default")
```

- If the grain inherits `Grain[TState]`, the decorator infers `state_type` from the generic type argument — the explicit parameter becomes optional.
- If `state_type` is omitted (or the grain inherits `Grain[None]`), the grain is stateless.
- `storage` names a registered storage provider (default: `"default"`).
- pyleans currently supports **one state object per grain**.

## Considered Options

- **Class-level decorator (Orleans legacy pattern)** — chosen. Fits Python's `@grain` idiom; single storage annotation is sufficient for PoC.
- **Constructor-parameter pattern (Orleans modern)** — supports multiple named state objects per grain, each backed by a different provider. Rejected for PoC. Can be added later as an additive feature without breaking existing grains.

## Consequences

- Grain authors write `@grain(storage="default")` on a `Grain[TState]` subclass — minimal boilerplate.
- Grains that need multiple state objects must be split, or wait until the multi-state pattern is added in a later phase.

## Related

- [adr-grain-base-class](adr-grain-base-class.md)
- [adr-grain-interfaces](adr-grain-interfaces.md)
- [adr-provider-interfaces](adr-provider-interfaces.md)
