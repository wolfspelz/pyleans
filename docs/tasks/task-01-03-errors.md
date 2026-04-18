# Task 01-03: Error Types

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-01-project-setup.md](task-01-01-project-setup.md)

## References
- [plan.md](../plan.md) -- Section 4 (Package Structure)
- [orleans-persistence.md](../orleans-architecture/orleans-persistence.md) -- ETags and optimistic concurrency

## Description

Define the exception hierarchy for pyleans.

### Files to create
- `src/pyleans/pyleans/errors.py`

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

- [x] All exceptions inherit from `PyleansError`
- [x] `StorageInconsistencyError` carries the expected and actual ETags
- [x] Importable from `pyleans.errors`

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Files created
- `src/pyleans/pyleans/errors.py` — Full exception hierarchy
- `src/pyleans/test/test_errors.py` — 23 tests

### Key decisions
- `StorageInconsistencyError` stores `expected_etag` and `actual_etag` as attributes and formats them in the message.
- All error classes use only `str | None` attributes — no complex types to keep them serializable.

### Deviations
- None.

### Test coverage
- 23 tests covering hierarchy (all subclass PyleansError), StorageInconsistencyError etag storage (both present, one None, both None), catchability at multiple levels, and message content for all grain error types.