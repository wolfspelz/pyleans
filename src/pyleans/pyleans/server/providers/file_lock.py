"""Cross-process file lock and atomic-write helper for file-based providers.

The YAML and Markdown membership providers both need:

1. **Cross-process mutual exclusion** on the membership file so two silos
   sharing a filesystem serialise their read-modify-write cycles.
2. **Atomic writes** so readers never see a partially written file — the
   YAML table must always parse cleanly even when a writer is in flight.

This module ships both primitives as a single, dependency-free, auditable
surface (~200 LOC). The providers call :class:`FileLock` as an ``async``
context manager and :func:`atomic_write` inside the critical section.

Platform dispatch:

* **POSIX**: :mod:`fcntl.flock` on a ``{path}.lock`` sidecar file.
* **Windows**: :func:`msvcrt.locking` on the same sidecar, byte-range-locked.

See :doc:`../../../../docs/tasks/task-02-10-file-locking-membership`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Any

from pyleans.providers.errors import MembershipUnavailableError

logger = logging.getLogger(__name__)

_DEFAULT_RETRY_INTERVAL = 0.05
_LOCK_SIDECAR_SUFFIX = ".lock"


class FileLock:
    """Cross-process advisory exclusive lock on a single file path.

    Used as an async context manager::

        async with FileLock(path, timeout=5.0):
            data = path.read_bytes()
            # modify...
            await atomic_write(path, new_data)

    The lock lives on a sidecar ``{path}.lock`` file so the primary file
    can be atomically replaced via ``os.replace`` without first releasing
    the lock. The sidecar file itself is never deleted — doing so would
    create a TOCTOU race in which two processes lock different inodes
    that happen to share the same pathname.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        timeout: float = 5.0,
        retry_interval: float = _DEFAULT_RETRY_INTERVAL,
    ) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout!r}")
        if retry_interval <= 0:
            raise ValueError(f"retry_interval must be positive, got {retry_interval!r}")
        self._path = Path(path)
        self._lock_path = Path(str(self._path) + _LOCK_SIDECAR_SUFFIX)
        self._timeout = timeout
        self._retry_interval = retry_interval
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def is_held(self) -> bool:
        return self._fd is not None

    async def __aenter__(self) -> FileLock:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.release()

    async def acquire(self) -> None:
        """Acquire the lock, polling every ``retry_interval`` until success or timeout.

        Raises:
            :class:`MembershipUnavailableError`: on timeout.
        """
        if self._fd is not None:
            raise RuntimeError("lock already acquired by this FileLock instance")
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout
        fd = os.open(
            str(self._lock_path),
            os.O_WRONLY | os.O_CREAT,
            0o600,
        )
        try:
            while True:
                if _try_lock(fd):
                    self._fd = fd
                    return
                if time.monotonic() >= deadline:
                    raise MembershipUnavailableError(f"file lock timeout on {self._path}")
                await asyncio.sleep(self._retry_interval)
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        """Release the lock if held. Idempotent / exception-safe."""
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            _release_lock(fd)
        finally:
            os.close(fd)


async def atomic_write(path: Path | str, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically.

    Strategy: write to a sibling ``{path}.<pid>.tmp`` file on the same
    filesystem, ``fsync`` it, then ``os.replace`` over the target.
    ``os.replace`` is guaranteed atomic on POSIX and on NTFS, provided
    source and destination are on the same filesystem — placing the
    temp file alongside the target ensures that.

    The temp file is removed on success (via the rename) and on failure
    (via an explicit unlink in the ``finally`` block). Only an uncaught
    SIGKILL between create and rename can leave it on disk.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.{os.getpid()}.tmp"
    try:
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            written = 0
            view = memoryview(data)
            while written < len(data):
                chunk = os.write(fd, view[written:])
                if chunk == 0:
                    raise OSError(f"short write to {tmp_path}: wrote {written}/{len(data)}")
                written += chunk
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp_path), str(target))
    except BaseException:
        # On any failure (including KeyboardInterrupt mid-write), clean up
        # the temp file rather than leaving a stale artefact on disk.
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def _try_lock(fd: int) -> bool:
    """Attempt a non-blocking exclusive lock on ``fd``. Return True on success."""
    if sys.platform == "win32":
        return _try_lock_windows(fd)
    return _try_lock_posix(fd)


def _release_lock(fd: int) -> None:
    if sys.platform == "win32":
        _release_lock_windows(fd)
    else:
        _release_lock_posix(fd)


def _try_lock_posix(fd: int) -> bool:
    import fcntl  # pylint: disable=import-outside-toplevel

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _release_lock_posix(fd: int) -> None:
    import fcntl  # pylint: disable=import-outside-toplevel

    fcntl.flock(fd, fcntl.LOCK_UN)


def _try_lock_windows(fd: int) -> bool:
    msvcrt = _load_msvcrt()
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _release_lock_windows(fd: int) -> None:
    msvcrt = _load_msvcrt()
    # Seek to the locked byte then unlock.
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _load_msvcrt() -> Any:
    """Import ``msvcrt`` lazily on Windows.

    Typed ``Any`` because the module is platform-conditional and mypy on
    POSIX reports attribute-defined errors on the ``locking`` / ``LK_*``
    constants — none of which exist on the non-Windows stub.
    """
    import msvcrt  # type: ignore[import-not-found,unused-ignore]  # pylint: disable=import-outside-toplevel,import-error

    return msvcrt
