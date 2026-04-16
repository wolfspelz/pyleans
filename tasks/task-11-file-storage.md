# Task 11: YAML/JSON File Storage Provider

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-05-provider-abcs.md](task-05-provider-abcs.md)
- [task-04-serialization.md](task-04-serialization.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Provider Interfaces, Serialization
- [orleans-persistence.md](../docs/orleans-persistence.md) -- IGrainStorage, ETags

## Description

Implement a file-based storage provider. One JSON file per grain, stored in a
configurable directory. Supports ETags for optimistic concurrency.

### Files to create
- `pyleans/pyleans/server/providers/file_storage.py`

### Design

```python
class FileStorageProvider(StorageProvider):
    """
    Stores grain state as individual JSON files.

    Directory layout:
        {base_path}/{grain_type}/{grain_key}.json

    Each file contains:
        {
            "etag": "uuid-string",
            "state": { ... grain state fields ... }
        }
    """

    def __init__(self, base_path: str = "./pyleans-data/storage"):
        self._base_path = Path(base_path)

    async def read(self, grain_type, grain_key) -> tuple[dict, str | None]:
        path = self._grain_path(grain_type, grain_key)
        if not path.exists():
            return {}, None
        data = orjson.loads(path.read_bytes())
        return data["state"], data["etag"]

    async def write(self, grain_type, grain_key, state, expected_etag) -> str:
        path = self._grain_path(grain_type, grain_key)
        # Check etag if file exists
        if path.exists():
            current = orjson.loads(path.read_bytes())
            if expected_etag is not None and current["etag"] != expected_etag:
                raise StorageInconsistencyError(expected_etag, current["etag"])
        new_etag = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(orjson.dumps({"etag": new_etag, "state": state}))
        return new_etag

    async def clear(self, grain_type, grain_key, expected_etag) -> None:
        path = self._grain_path(grain_type, grain_key)
        if path.exists():
            if expected_etag is not None:
                current = orjson.loads(path.read_bytes())
                if current["etag"] != expected_etag:
                    raise StorageInconsistencyError(expected_etag, current["etag"])
            path.unlink()

    def _grain_path(self, grain_type: str, grain_key: str) -> Path:
        # Sanitize key for filesystem safety
        safe_key = grain_key.replace("/", "_").replace("\\", "_")
        return self._base_path / grain_type / f"{safe_key}.json"
```

### Notes
- File I/O is synchronous but fast for small JSON. For PoC this is acceptable.
  Could use `aiofiles` later if needed.
- ETags use UUID4 -- simple and unique.
- Directory created on first write.
- File paths sanitized to prevent directory traversal.

### Acceptance criteria

- [x] Read returns `({}, None)` for non-existent grain
- [x] Write creates file with state and etag
- [x] Read returns correct state and etag after write
- [x] Write with wrong etag raises `StorageInconsistencyError`
- [x] Write with `expected_etag=None` always succeeds (first write)
- [x] Clear removes the file
- [x] Directory structure: `{base_path}/{grain_type}/{key}.json`
- [x] Integration test: round-trip read/write/clear

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Files created
- `pyleans/pyleans/server/providers/file_storage.py` — FileStorageProvider
- `pyleans/test/test_file_storage.py` — 15 tests

### Key decisions
- One JSON file per grain: `{base_path}/{grain_type}/{safe_key}.json`.
- ETags use UUID4.
- Key sanitization replaces `/`, `\\`, `..` with `_` to prevent path traversal.
- Synchronous file I/O (acceptable for PoC, small JSON files).

### Deviations
- None.

### Test coverage
- 15 tests: read nonexistent, read after write, first write, directory creation, correct/wrong/none etag writes, unique etags, clear (with/without etag, nonexistent), directory structure, key sanitization, full lifecycle round-trip.