# Task 04: Serialization

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-project-setup.md](task-01-project-setup.md)
- [task-03-errors.md](task-03-errors.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Decision 3 (Serialization)
- [orleans-networking.md](../docs/orleans-networking.md) -- Serialization System

## Description

Implement a pluggable serialization layer with JSON/orjson as the default.

### Files to create
- `src/pyleans/serialization.py`

### Design

```python
class Serializer(ABC):
    """Pluggable serialization interface."""
    @abstractmethod
    def serialize(self, obj: Any) -> bytes: ...

    @abstractmethod
    def deserialize(self, data: bytes, target_type: type[T]) -> T: ...

class JsonSerializer(Serializer):
    """Default JSON serializer using orjson."""

    def serialize(self, obj: Any) -> bytes:
        # Handle dataclasses via dataclasses.asdict()
        # Use orjson.dumps for speed

    def deserialize(self, data: bytes, target_type: type[T]) -> T:
        # orjson.loads -> dict -> target_type(**dict) for dataclasses
        # Direct return for primitives
```

### Requirements
- Serialize/deserialize `@dataclass` instances
- Serialize/deserialize primitive types (str, int, float, bool, list, dict)
- Handle nested dataclasses
- Handle `None`
- Raise `SerializationError` on failure

### Acceptance criteria

- [ ] Round-trip dataclass serialization works
- [ ] Nested dataclasses serialize correctly
- [ ] Primitives serialize correctly
- [ ] `SerializationError` raised for non-serializable types
- [ ] `Serializer` ABC allows alternative implementations

## Summary of implementation

### Files created
- `pyleans/pyleans/serialization.py` — Serializer ABC, JsonSerializer with orjson
- `pyleans/test/test_serialization.py` — 22 tests

### Key decisions
- `_dataclass_to_dict` recursively converts nested dataclasses, lists, and dicts.
- `_dict_to_dataclass` reconstructs nested dataclasses by inspecting field types.
- String type annotations are skipped during nested reconstruction (no eval).

### Deviations
- None.

### Test coverage
- 22 tests: ABC abstractness, custom implementations, all primitive types, dataclass round-trips (simple, nested, with lists, defaults), output format, and all error cases.