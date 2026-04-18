# Task 17: Counter App (Standalone Silo)

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-15-silo.md](task-15-silo.md)
- [task-16-counter-grain.md](task-16-counter-grain.md)
- [task-11-file-storage.md](task-11-file-storage.md)
- [task-12-yaml-membership.md](task-12-yaml-membership.md)

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
- `src/counter_app/main.py`
- `src/counter_app/answer_grain.py` (one grain per file)

### AnswerGrain

A minimal stateless grain to demonstrate hosting multiple grains in one silo:

```python
# src/counter_app/answer_grain.py
from pyleans import grain

@grain
class AnswerGrain:
    """A stateless grain that always returns 42."""

    async def get(self) -> int:
        return 42
```

### main.py

```python
"""Standalone silo hosting CounterGrain and AnswerGrain."""

import asyncio
from pyleans.server import Silo
from pyleans.server.grains import system_grains
from pyleans.server.providers import FileStorageProvider, YamlMembershipProvider
from src.counter_app.counter_grain import CounterGrain
from src.counter_app.answer_grain import AnswerGrain

async def main() -> None:
    silo = Silo(
        grains=[CounterGrain, AnswerGrain, *system_grains()],
        storage_providers={"default": FileStorageProvider("./data/storage")},
        membership_provider=YamlMembershipProvider("./data/membership.yaml"),
    )
    await silo.start()  # blocks until Ctrl+C

if __name__ == "__main__":
    asyncio.run(main())
```

### CounterGrain additions

The CounterGrain receives `SiloManagement` via constructor injection
(type-hint constructor injection) and exposes silo metadata:

```python
@grain(state_type=CounterState, storage="default")
class CounterGrain:
    
    def __init__(self, silo_mgmt: SiloManagement ):
        self._silo_mgmt = silo_mgmt

    # ... existing methods ...

    async def get_silo_info(self) -> dict[str, object]:
        """Return metadata about the silo hosting this grain."""
        return self._silo_mgmt.get_info()
```

### What this demonstrates

- Silo as a standalone process (the default deployment model)
- `asyncio.run()` as the entry point
- Multiple grain classes registered at startup (CounterGrain, AnswerGrain)
- State persisted to files (survives restart)
- Membership visible in YAML file
- Ctrl+C triggers graceful shutdown via signal handling
- `system_grains()` included for future framework grains
- Grains access silo metadata via DI-injected `SiloManagement` service (``)

### Acceptance criteria

- [x] `python -m src.counter_app` starts silo and blocks
- [x] Ctrl+C triggers graceful shutdown (deactivates grains, saves state)
- [x] `data/membership.yaml` shows silo entry while running
- [x] `data/membership.yaml` shows no silo entry after clean shutdown
- [x] Grain state files created under `data/storage/CounterGrain/`
- [x] Integration test: start silo, call grain via runtime, stop silo, verify state persisted
- [x] `AnswerGrain.get()` returns 42 (stateless grain, no persistence)
- [x] `CounterGrain.get_silo_info()` returns silo metadata via DI-injected `SiloManagement`
- [x] Silo info queryable from ClusterClient via the gateway

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
- `src/counter_app/main.py` — standalone silo entry point (existed from prior task, unchanged)
- `src/counter_app/test/test_counter_app.py` — 9 integration tests using real file-based providers

### Key decisions
- Tests use `tmp_path` pytest fixture for isolation (no shared state between tests)
- Tests use `start_background()` instead of `start()` to avoid blocking
- `InMemoryStreamProvider` used in tests since streaming isn't the focus of this task

### Test coverage (9 tests)
- **TestSiloStartStop**: membership file created on start, cleared on shutdown, stop idempotent
- **TestGrainStatePersistence**: state file created, state survives restart, separate files per grain
- **TestGrainLifecycle**: deactivation saves state, shutdown deactivates all grains
- **TestFullIntegration**: end-to-end lifecycle (start → call grains → stop → verify persistence → restart → verify state)
