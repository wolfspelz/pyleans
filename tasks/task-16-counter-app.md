# Task 16: Counter App (Standalone Silo)

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-14-silo.md](task-14-silo.md)
- [task-15-counter-grain.md](task-15-counter-grain.md)
- [task-10-file-storage.md](task-10-file-storage.md)
- [task-11-yaml-membership.md](task-11-yaml-membership.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Phase 1 milestone, Section 7 (Silo usage)

## Description

Create a standalone silo application that hosts the CounterGrain.
This is the default deployment model: the silo runs as its own process.
External clients connect via the gateway protocol (see task 17).

**Note:** The Silo class supports co-hosting (e.g. with FastAPI via
`start_background()`), but the default and recommended deployment is a
standalone silo process. Co-hosting is an advanced pattern for when the
silo and a web API must share a process.

### Files to create
- `counter-app/counter/main.py`

### main.py

```python
"""Standalone silo hosting CounterGrain."""

import asyncio
from pyleans.server import Silo
from pyleans.server.providers import FileStorageProvider, YamlMembershipProvider
from counter.grains import CounterGrain

async def main() -> None:
    silo = Silo(
        grains=[CounterGrain],
        storage_providers={"default": FileStorageProvider("./data/storage")},
        membership_provider=YamlMembershipProvider("./data/membership.yaml"),
    )
    await silo.start()  # blocks until Ctrl+C

if __name__ == "__main__":
    asyncio.run(main())
```

### What this demonstrates

- Silo as a standalone process (the default deployment model)
- `asyncio.run()` as the entry point
- Grain classes registered at startup
- State persisted to files (survives restart)
- Membership visible in YAML file
- Ctrl+C triggers graceful shutdown via signal handling

### Acceptance criteria

- [ ] `python -m counter.main` starts silo and blocks
- [ ] Ctrl+C triggers graceful shutdown (deactivates grains, saves state)
- [ ] `data/membership.yaml` shows silo entry while running
- [ ] `data/membership.yaml` shows no silo entry after clean shutdown
- [ ] Grain state files created under `data/storage/CounterGrain/`
- [ ] Integration test: start silo, call grain via runtime, stop silo, verify state persisted

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
