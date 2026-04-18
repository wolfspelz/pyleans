---
status: accepted
date: 2026-04-18
tags: [distribution, packaging, dx]
---

# pyleans Is a Library, Not a CLI Tool

## Context and Problem Statement

Some frameworks ship CLI tools to scaffold projects, generate code, and run the silo (e.g. `rails new`, `dotnet new orleans`). Others are libraries users wire up themselves (Flask, FastAPI). We need to choose one deployment story.

## Decision

pyleans is a library. Requires **Python 3.12+**. The user writes their own `main.py`, creates a `Silo`, and calls `silo.start()`. This gives full control over DI setup, co-hosting with FastAPI, and custom startup logic. No code generation, no scaffolding commands.

```python
# my_app/main.py
import asyncio
from pyleans.server import Silo
from my_grains import PlayerGrain, RoomGrain

silo = Silo(
    grains=[PlayerGrain, RoomGrain],
)
asyncio.run(silo.start())
```

```bash
python my_app/main.py

# Multi-core: run multiple silos on different ports
python my_app/main.py --port 11111 &
python my_app/main.py --port 11112 &
```

## Considered Options

- **Library-only, user-owned `main.py`** — chosen. Maximum flexibility, no magic.
- **CLI scaffolding (`pyleans new`, `pyleans run`)** — rejected. Adds maintenance burden, freezes project layout, obscures how the framework composes.

## Consequences

- Users see plain Python from day one — easier to reason about and co-host with web frameworks.
- Slightly more boilerplate at project start, but nothing pyleans-specific.
- No generated files to keep in sync with framework upgrades.

## Related

- [adr-package-split](adr-package-split.md)
- [adr-dev-mode](adr-dev-mode.md)
