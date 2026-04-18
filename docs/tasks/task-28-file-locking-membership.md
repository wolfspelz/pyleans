# Task 28: File Locking for File-Based Membership Providers

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-27-membership-table-extensions.md](task-27-membership-table-extensions.md)

## References
- [plan.md](../plan.md) -- Phase 2 item 3 (file locking)
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §4.2 consistency requirements (atomic multi-row updates)
- Research: POSIX `fcntl.flock`, Windows `msvcrt.locking`, Python `portalocker` library, atomic-rename idiom

## Description

Multi-silo clusters on a shared filesystem (e.g. a developer running two silos against one YAML membership file, or two silos in the same container sharing a volume) will corrupt the membership file if both write at the same time without coordination. The YAML and Markdown providers need a cross-process lock during read-modify-write.

This task introduces a **single file-locking primitive** used by both file-based providers, plus the atomic-rename write idiom for durability. Abstracting it once avoids re-implementing it (and re-testing it) in each provider.

### Files to create

- `src/pyleans/pyleans/server/providers/file_lock.py`

### Design

```python
class FileLock:
    """Cross-process advisory lock on a single file path.

    On POSIX: uses fcntl.flock on a separate ".lock" sidecar file.
    On Windows: uses msvcrt.locking with polling retry.

    Used as an async context manager:

        async with FileLock(membership_path, timeout=5.0):
            # exclusive access — safe to read-modify-write the file
            ...
    """

    def __init__(self, path: Path, timeout: float = 5.0) -> None: ...

    async def __aenter__(self) -> None:
        """Acquire the lock. Raises asyncio.TimeoutError after `timeout`.
        Async-friendly: yields to the event loop between retries."""

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Release the lock. Safe to call after any exception."""


async def atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically.

    Algorithm:
        1. Write to a sibling temp file (same directory, same filesystem).
        2. fsync the temp file.
        3. os.replace(temp, path) — atomic on both POSIX and Windows.

    The temp file is created with mode 0600 on POSIX. os.replace over
    a lock file is not used; the lock file lives beside the data file.
    """
```

### Platform-specific primitives

**POSIX** (`fcntl`):
- Sidecar lock file: `{path}.lock`. Opened with `os.open(..., O_WRONLY | O_CREAT)`.
- `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` — non-blocking try.
- On `BlockingIOError`, sleep `retry_interval` (default 50 ms) and try again until timeout.
- Release via `flock(fd, LOCK_UN)` then `os.close(fd)`. Do NOT delete the lock file (another process may hold the fd, and removing creates a TOCTOU race where two processes lock different inodes with the same name).

**Windows** (`msvcrt`):
- Same sidecar approach.
- `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` — locks 1 byte at offset 0. Retry with sleep on `OSError`.
- Release via `LK_UNLCK`.

**Dispatch**: a thin `sys.platform`-based factory inside `FileLock.__aenter__`. One code path per OS keeps mental model simple; no cross-platform library dependency needed.

### Why not `portalocker` or `filelock`?

Both libraries are widely used and do this correctly. Reasons to hand-roll instead:

1. **Zero new dependency** -- matches CLAUDE.md KISS / "prefer stdlib" guidance for small surfaces.
2. **Async-aware retry** -- `filelock` is sync and blocks the event loop under contention. We want cooperative yielding.
3. **Small, auditable code** -- ~80 LOC including tests, straightforward to review for correctness.

If this surface grows (e.g. adding shared-read locks for snapshot reads), revisit the decision and bring in `filelock`.

### Atomic rename contract

`os.replace(temp, target)` is guaranteed atomic on both POSIX (rename(2)) and Windows NTFS (Windows MoveFileEx with MOVEFILE_REPLACE_EXISTING). Reader processes always see either the old file or the new -- never a partially written one. This is why the YAML/Markdown providers MUST funnel every write through `atomic_write`.

**Caveat**: atomicity holds only within the same filesystem. If `path` and `path.tmp` end up on different mounts (can happen with weird bind mounts), `os.replace` falls back to copy+delete, which is not atomic. `atomic_write` resolves the temp path within the target's parent directory to avoid this -- tested on both POSIX and Windows.

### Lock scope

The lock guards a single file. Providers must:

1. Acquire the lock.
2. Read the file (or default to empty table if absent).
3. Compute the new state (including ETag check from [task-27](task-27-membership-table-extensions.md)).
4. `atomic_write` the new state.
5. Release the lock.

**ETag check happens under the lock**, not before. If providers checked ETags before taking the lock, two processes could both see the same etag, both take the lock in turn, and both succeed -- the second write silently overwriting the first. Mandatory: etag check strictly inside the critical section.

### Fail-safe on lock timeout

A 5-second default timeout protects against a crashed silo that holds the lock. Long-term fix is per-process lock ownership tracking; for PoC, the timeout-then-error approach surfaces the problem loudly (MembershipError bubbles up) rather than silently deadlocking the cluster.

When timeout expires, the provider raises `MembershipUnavailableError("file lock timeout on <path>")`. The silo's failure detector ([task-29](task-29-failure-detector.md)) interprets this as "table unavailable" and waits rather than voting anyone dead.

### Acceptance criteria

- [ ] `FileLock` acquires and releases cleanly on POSIX
- [ ] `FileLock` acquires and releases cleanly on Windows
- [ ] Two async tasks in the same process serialize through the lock (second task waits for first to release)
- [ ] Two subprocesses contending on the same lock file serialize (integration test using `multiprocessing`)
- [ ] `FileLock` times out after `timeout` seconds with `asyncio.TimeoutError`
- [ ] `atomic_write` round-trips arbitrary bytes to disk
- [ ] `atomic_write` does NOT leave the temp file on disk in any success or failure path (except uncaught SIGKILL)
- [ ] Reader sees either old or new content, never partial -- tested with a write-while-reading loop
- [ ] YAML and Markdown providers use `FileLock` + `atomic_write` for all mutating operations
- [ ] Unit tests cover: timeout, concurrent acquire, atomic write, temp-file cleanup on exception

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
