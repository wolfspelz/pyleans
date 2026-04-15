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
pyleans/
  pyproject.toml
  src/
    pyleans/
      __init__.py
      py.typed           # PEP 561 marker
      server/
        __init__.py
      client/
        __init__.py
      providers/
        __init__.py
  tests/
    __init__.py
  examples/
    counter-app/
    counter-client/
```

### pyproject.toml

- `[project]`: name=pyleans, requires-python=">=3.12"
- `[build-system]`: hatchling
- Dependencies: `dependency-injector`, `orjson`, `pyyaml`
- Optional dependencies: `[web]` = `fastapi`, `uvicorn`
- Dev dependencies: `pytest`, `pytest-asyncio`, `ruff`, `mypy`
- Package manager: `pip` with `venv`

### Acceptance criteria

- [ ] `pip install -e "pyleans[dev]"` succeeds
- [ ] `import pyleans` works
- [ ] `import pyleans.server` works
- [ ] `import pyleans.client` works
- [ ] `pytest` runs (no tests yet, but framework works)

## Summary of implementation

### Files created/modified
- `pyleans/pyproject.toml` — Updated with full dependencies (`dependency-injector`, `orjson`, `pyyaml`), optional `[web]` deps (`fastapi`, `uvicorn`), `[dev]` deps (`pytest`, `pytest-asyncio`, `pytest-cov`, `mypy`, `ruff`), hatch build targets, and asyncio_mode config.
- `pyleans/pyleans/server/__init__.py` — Created server subpackage.
- `pyleans/pyleans/client/__init__.py` — Created client subpackage.
- `pyleans/pyleans/providers/__init__.py` — Created providers subpackage.

### Key decisions
- Kept `counter-app/` and `counter-client/` at workspace root (matching the plan doc Section 4) rather than nesting under `pyleans/examples/`.
- Used `[tool.hatch.build.targets.wheel] packages = ["pyleans"]` so hatchling finds the package correctly in the flat layout.

### Deviations
- Task spec shows `src/pyleans/` layout but existing code uses flat `pyleans/pyleans/` layout. Kept the existing layout.

### Test coverage
- No tests yet (as expected for this task). pytest framework runs successfully.