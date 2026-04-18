---
status: accepted
date: 2026-04-18
tags: [serialization, storage, wire-format]
---

# JSON Serialization via `dataclasses` and `orjson`

## Context and Problem Statement

pyleans needs a serialization format for:

- grain state persisted to storage;
- method arguments and results crossing the gateway protocol;
- stream events.

Requirements: fast, human-readable (for debugging in dev mode), no external schema compiler, safe on untrusted input, pluggable so production deployments can swap the format.

## Decision

JSON via `dataclasses` + `orjson`, behind a pluggable `Serializer` ABC.

- Grain state must be a `@dataclass`.
- Method arguments must be JSON-serializable types.
- Enforced by convention, not by runtime type checks.
- Implementations can be swapped (e.g. msgpack for production) via the `Serializer` port.

```python
@dataclass
class PlayerState:
    name: str = ""
    level: int = 1
    inventory: list[str] = field(default_factory=list)

@grain(state_type=PlayerState, storage="default")
class PlayerGrain(Grain[PlayerState]):
    async def get_name(self) -> str:
        return self.state.name
```

## Considered Options

- **JSON + dataclasses + orjson** — chosen. Human-readable for dev storage, fast (orjson), no schema compilation, stdlib-native types.
- **MessagePack** — fast and compact but not human-readable; debugging dev storage becomes harder. Rejected for default; valid as a pluggable choice for production.
- **pickle** — Python-only and unsafe on untrusted input (arbitrary code execution on deserialize). Rejected outright.
- **Protocol Buffers** — requires `.proto` compilation and schema management. Rejected for PoC; valid choice for polyglot production.

## Consequences

- Dev storage files are readable and editable by hand.
- Pluggable `Serializer` means production deployments can switch formats without grain code changes.
- No support for arbitrary Python objects — grains must use serializable types.

## Related

- [adr-provider-interfaces](adr-provider-interfaces.md) — providers depend on the `Serializer` port.
- [adr-state-declaration](adr-state-declaration.md)
