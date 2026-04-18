---
status: accepted
date: 2026-04-18
tags: [packaging, client-server]
---

# One Package, Two Entry Points (`pyleans.server` and `pyleans.client`)

## Context and Problem Statement

Silo code pulls in the runtime, DI container, all providers, and transport — not cheap. External clients (web servers, CLIs, other services) only need the gateway protocol and serialization. Importing silo internals into a web app process is wasteful and drags in unwanted transitive dependencies.

## Decision

One pip package (`pyleans`), two entry points:

- `pyleans.server` — silo runtime (heavy: runtime, providers, DI container, transport).
- `pyleans.client` — lightweight client library (gateway protocol + serialization only).

Client code must avoid importing the heavy silo runtime. A web server that calls grains only needs `from pyleans.client import ClusterClient`.

```python
# web_server/api.py — e.g. a FastAPI app that calls grains
from pyleans.client import ClusterClient

client = ClusterClient(gateways=["localhost:30000"])
await client.connect()

player = client.get_grain(PlayerGrain, "player-42")
name = await player.get_name()
```

## Considered Options

- **One package, two entry points (`server` / `client`)** — chosen. Single install, clear separation by import surface.
- **Two separate packages (`pyleans-server`, `pyleans-client`)** — more independence, but doubles release coordination and version drift risk. Rejected for PoC; can be split later if dependency weight diverges further.

## Consequences

- One install covers both roles (silo operator, client author).
- Both modules must be kept import-isolated — CI should verify that importing `pyleans.client` does not transitively pull in `pyleans.server`.
- Shared code (`grain.py`, `identity.py`, serialization, gateway protocol) lives at the package root and is imported by both.

## Related

- [adr-library-vs-cli](adr-library-vs-cli.md)
