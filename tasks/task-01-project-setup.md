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
_To be filled when task is complete._