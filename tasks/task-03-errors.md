# Task 03: Error Types

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-project-setup.md](task-01-project-setup.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Section 4 (errors.py)
- [orleans-persistence.md](../docs/orleans-persistence.md) -- ETags and optimistic concurrency

## Description

Define the exception hierarchy for pyleans.

### Files to create
- `src/pyleans/errors.py`

### Exception classes

```python
class PyleansError(Exception):
    """Base class for all pyleans errors."""

class GrainError(PyleansError):
    """Error related to grain operations."""

class GrainNotFoundError(GrainError):
    """Grain type not registered with the silo."""

class GrainActivationError(GrainError):
    """Failed to activate a grain."""

class GrainDeactivationError(GrainError):
    """Failed to deactivate a grain."""

class GrainMethodError(GrainError):
    """Error invoking a grain method."""

class StorageError(PyleansError):
    """Error in a storage provider."""

class StorageInconsistencyError(StorageError):
    """ETag mismatch -- optimistic concurrency violation."""

class MembershipError(PyleansError):
    """Error in the membership provider."""

class TransportError(PyleansError):
    """Error in the transport layer."""

class SerializationError(PyleansError):
    """Error serializing or deserializing data."""
```

### Acceptance criteria

- [ ] All exceptions inherit from `PyleansError`
- [ ] `StorageInconsistencyError` carries the expected and actual ETags
- [ ] Importable from `pyleans.errors`

## Summary of implementation
_To be filled when task is complete._