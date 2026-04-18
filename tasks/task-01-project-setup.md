# Task 01: Project Setup

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
None -- this is the first task.

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Section 4 (Package Structure), Section 7 (Resolved Questions)

## Description

Create the pyleans project skeleton with modern Python packaging.

### What to create

```
src/
  pyleans/                  # framework project dir (pip-installed editable)
    pyproject.toml
    pyleans/                # importable package (flat inner layout)
      __init__.py
      py.typed              # PEP 561 marker
      server/
        __init__.py
      client/
        __init__.py
      providers/
        __init__.py
    test/
  counter_app/              # sample apps (not pip-installed)
  counter_client/
```

### pyproject.toml

- `[project]`: name=pyleans, requires-python=">=3.12"
- `[build-system]`: hatchling
- Dependencies: `injector`, `orjson`, `pyyaml`
- Optional dependencies: `[web]` = `fastapi`, `uvicorn`
- Dev dependencies: `pytest`, `pytest-asyncio`, `ruff`, `mypy`
- Package manager: `pip` with `venv`

### Acceptance criteria

- [x] `pip install -e "src/pyleans[dev]"` succeeds
- [x] `import pyleans` works
- [x] `import pyleans.server` works
- [x] `import pyleans.client` works
- [x] `pytest` runs (no tests yet, but framework works)

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Files created/modified
- `src/pyleans/pyproject.toml` — Updated with full dependencies (`injector`, `orjson`, `pyyaml`), optional `[web]` deps (`fastapi`, `uvicorn`), `[dev]` deps (`pytest`, `pytest-asyncio`, `pytest-cov`, `mypy`, `ruff`), hatch build targets, and asyncio_mode config.
- `src/pyleans/pyleans/server/__init__.py` — Created server subpackage.
- `src/pyleans/pyleans/client/__init__.py` — Created client subpackage.
- `src/pyleans/pyleans/providers/__init__.py` — Created providers subpackage.

### Key decisions
- Placed `counter_app/` and `counter_client/` under `src/` at workspace root (run as `python -m src.counter_app`) rather than nesting under `pyleans/examples/`.
- Used `[tool.hatch.build.targets.wheel] packages = ["pyleans"]` so hatchling finds the package correctly in the flat layout.

### Deviations
- Framework lives at `src/pyleans/` (project dir) with a flat inner package at `src/pyleans/pyleans/` — pip-installed editable, so imports are still bare `import pyleans`.
- Task originally nested examples under `pyleans/examples/` — moved `counter_app/` and `counter_client/` under `src/` at workspace root.
- Test directory is `test/` (singular), not `tests/`.

### Test coverage
- No tests yet (as expected for this task). pytest framework runs successfully.