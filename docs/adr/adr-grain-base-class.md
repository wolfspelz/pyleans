---
status: accepted
date: 2026-04-18
tags: [grain, runtime, api-surface]
---

# Grain Base Class `Grain[TState]`

## Context and Problem Statement

Stateful grains need runtime-bound attributes (identity, state, `read_state` / `write_state` / `clear_state`, logger, idle control). Orleans provides these via the generic `Grain<TState>` base class. The Python equivalent must:

- make `identity` available from `__init__` onward (Orleans exposes it via `IGrainContext.GrainId` in the constructor);
- integrate with the async runtime (storage load before `on_activate`);
- keep authoring overhead low — no DI ceremony for intrinsic attributes.

## Decision

Stateful grains inherit from `Grain[TState]`, a generic base class. Only one type parameter (state) is needed because grain keys are always strings (see [adr-state-declaration](adr-state-declaration.md)).

The base class provides:

- `identity: GrainId` — read-only property, available from `__init__` onward. Backed by a `contextvars.ContextVar` during construction and a permanent instance attribute after activation.
- `state: TState` — loaded from storage before `on_activate`.
- `read_state()` — refresh `state` from the storage provider (async). The runtime reads storage once at activation time (see [adr-state-declaration](adr-state-declaration.md) and [domain/0001-grains-are-an-active-cache](../domain/0001-grains-are-an-active-cache.md)); `read_state()` is the explicit escape hatch for a grain that must reconcile with out-of-band storage changes.
- `write_state()` — persist current state (async).
- `clear_state()` — clear persisted state and reset to defaults (async).
- `deactivate_on_idle()` — request deactivation after the current turn (sync).
- `logger` — per-grain-type logger (`pyleans.grain.<GrainType>`), read-only property.

Stub methods raise `GrainActivationError` if called before activation. The runtime overrides them with `setattr()` during activation, binding closures that capture the activation context (storage provider, etag, etc.).

The `logger` property returns `logging.getLogger(f"pyleans.grain.{type(self).__name__}")` — always available, no activation required.

```python
@grain(storage="default")
class CounterGrain(Grain[CounterState]):
    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.write_state()
        self.logger.debug("Counter incremented to %d", self.state.value)
        return self.state.value
```

The `@grain` decorator infers `state_type` from the generic type argument, so `@grain(storage="default")` suffices. Explicit `state_type` takes precedence if both are provided.

Stateless grains may optionally inherit `Grain[None]` if they need `identity` or `deactivate_on_idle`, but are not required to.

## Considered Options

- **Generic base class `Grain[TState]` with property-based `logger`** — chosen. Matches Orleans mental model, no constructor boilerplate.
- **Constructor-inject the logger via DI** — rejected. Works technically (the runtime knows the grain type and could override a container provider per activation), but adds boilerplate (`@inject`, `__init__`, `Provide[...]`) with no benefit. The logger is determined entirely by the class name, not by configuration or environment. A property is the right abstraction for an intrinsic attribute of the grain type.

## Consequences

- Porting Orleans grains is mechanical — names and shape match.
- `identity` works in `__init__`, enabling logging and DI setup at construction time.
- Adding a second type parameter later (e.g. for non-string keys) would be a breaking change. Acceptable given string keys are the only supported key type by design.

## Related

- [adr-state-declaration](adr-state-declaration.md)
- [adr-grain-interfaces](adr-grain-interfaces.md)
- [adr-logging](adr-logging.md)
