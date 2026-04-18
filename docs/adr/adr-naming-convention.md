---
status: accepted
date: 2026-04-18
tags: [api-surface, naming]
---

# Python Naming Convention Mapped from Orleans

## Context and Problem Statement

Orleans' public API uses C# conventions: PascalCase and an `Async` suffix on async methods. Copying them verbatim into Python conflicts with PEP 8, and `async def` already marks methods as async — a suffix is redundant. Inventing entirely new names loses familiarity for Orleans users.

## Decision

pyleans method and property names follow Orleans **semantics** but apply Python **conventions**:

- snake_case instead of PascalCase.
- No `Async` suffix on async methods.

**Mapping**:

| Orleans C# | pyleans Python | Notes |
|---|---|---|
| `State` (property) | `state` | snake_case |
| `WriteStateAsync()` | `write_state()` | no Async suffix |
| `ReadStateAsync()` | *(auto on activation)* | not exposed; same as Orleans default |
| `ClearStateAsync()` | `clear_state()` | no Async suffix |
| `DeactivateOnIdle()` | `deactivate_on_idle()` | snake_case |
| `OnActivateAsync()` | `on_activate()` | snake_case, no Async |
| `OnDeactivateAsync()` | `on_deactivate()` | snake_case, no Async |
| `GetPrimaryKeyString()` | `identity` (GrainId) | simplified — single key type |
| `GrainFactory.GetGrain<T>(key)` | `grain_factory.get_grain(T, key)` | snake_case |
| `RegisterTimer()` | `register_timer()` | snake_case |

This convention applies to all public API surfaces. Internal implementation names follow standard Python conventions without requiring Orleans alignment.

## Consequences

- Orleans users recognise familiar concepts under idiomatic Python names.
- Porting Orleans docs and examples is mechanical.
- Internal code is free to use Pythonic names even where Orleans has no analogue.

## Related

- [adr-grain-base-class](adr-grain-base-class.md)
- [adr-grain-interfaces](adr-grain-interfaces.md)
