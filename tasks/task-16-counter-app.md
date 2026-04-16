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
- `counter-app/counter_app/main.py`

### main.py

```python
"""Standalone silo hosting CounterGrain."""

import asyncio
from pyleans.server import Silo
from pyleans.server.grains import system_grains
from pyleans.server.providers import FileStorageProvider, YamlMembershipProvider
from counter_app.grains import CounterGrain

async def main() -> None:
    silo = Silo(
        grains=[CounterGrain, *system_grains()],
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
- `SiloGrain` included via `system_grains()` — explicit opt-in for framework management grains

### Acceptance criteria

- [x] `python -m counter_app` starts silo and blocks
- [x] Ctrl+C triggers graceful shutdown (deactivates grains, saves state)
- [x] `data/membership.yaml` shows silo entry while running
- [x] `data/membership.yaml` shows no silo entry after clean shutdown
- [x] Grain state files created under `data/storage/CounterGrain/`
- [x] Integration test: start silo, call grain via runtime, stop silo, verify state persisted
- [ ] `SiloGrain` included via `system_grains()` and queryable via gateway
- [ ] `SiloGrain.get_info()` returns silo metadata (silo_id, host, gateway_port, grain_count, etc.)

## Findings of code review

- [x] Unused `asyncio` import in test file — removed.
- No other issues. Code follows CLAUDE.md standards: clean naming, type hints, hexagonal architecture, SOLID.

## Findings of security review

- No issues found.
- `FileStorageProvider` sanitizes path components and validates against path traversal.
- `YamlMembershipProvider` uses `yaml.safe_load` (safe against code injection).
- Hardcoded data paths in `main.py` — no user-input-driven file access.
- No network listeners in Phase 1.

## Summary of implementation

### Files created
- `counter-app/counter_app/main.py` — standalone silo entry point (existed from prior task, unchanged)
- `counter-app/test/test_counter_app.py` — 9 integration tests using real file-based providers

### Key decisions
- Tests use `tmp_path` pytest fixture for isolation (no shared state between tests)
- Tests use `start_background()` instead of `start()` to avoid blocking
- `InMemoryStreamProvider` used in tests since streaming isn't the focus of this task

### Test coverage (9 tests)
- **TestSiloStartStop**: membership file created on start, cleared on shutdown, stop idempotent
- **TestGrainStatePersistence**: state file created, state survives restart, separate files per grain
- **TestGrainLifecycle**: deactivation saves state, shutdown deactivates all grains
- **TestFullIntegration**: end-to-end lifecycle (start → call grains → stop → verify persistence → restart → verify state)
