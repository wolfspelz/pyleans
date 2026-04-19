"""Tests for :mod:`pyleans.server.providers.file_lock`.

Covers the single-process serialisation contract, timeout behaviour,
:func:`atomic_write` round-trip and cleanup, and a cross-process
integration test via :mod:`multiprocessing`.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest
from pyleans.providers.errors import MembershipUnavailableError
from pyleans.server.providers.file_lock import FileLock, atomic_write


class TestFileLockAcquireRelease:
    async def test_acquire_release_roundtrip(self, tmp_path: Path) -> None:
        # Arrange
        lock = FileLock(tmp_path / "data.yaml", timeout=1.0)

        # Act
        await lock.acquire()
        held = lock.is_held
        lock.release()

        # Assert
        assert held is True
        assert lock.is_held is False

    async def test_context_manager_releases_even_on_exception(self, tmp_path: Path) -> None:
        # Arrange
        lock = FileLock(tmp_path / "data.yaml", timeout=1.0)

        # Act / Assert
        with pytest.raises(RuntimeError):
            async with lock:
                raise RuntimeError("boom")
        assert lock.is_held is False

    async def test_release_without_acquire_is_noop(self, tmp_path: Path) -> None:
        # Arrange
        lock = FileLock(tmp_path / "data.yaml", timeout=1.0)

        # Act / Assert (must not raise)
        lock.release()

    async def test_double_acquire_raises_runtime_error(self, tmp_path: Path) -> None:
        # Arrange
        lock = FileLock(tmp_path / "data.yaml", timeout=1.0)
        await lock.acquire()
        try:
            # Act / Assert
            with pytest.raises(RuntimeError):
                await lock.acquire()
        finally:
            lock.release()

    async def test_sidecar_lock_file_is_created_next_to_target(self, tmp_path: Path) -> None:
        # Arrange
        target = tmp_path / "data.yaml"
        lock = FileLock(target, timeout=1.0)

        # Act
        await lock.acquire()
        try:
            # Assert
            assert lock.lock_path == tmp_path / "data.yaml.lock"
            assert lock.lock_path.exists()
        finally:
            lock.release()


class TestFileLockSerialisation:
    async def test_second_acquirer_waits_for_first_to_release(self, tmp_path: Path) -> None:
        # Arrange
        target = tmp_path / "data.yaml"
        first = FileLock(target, timeout=2.0, retry_interval=0.01)
        second = FileLock(target, timeout=2.0, retry_interval=0.01)
        order: list[str] = []

        async def first_task() -> None:
            async with first:
                order.append("first-acquired")
                await asyncio.sleep(0.1)
                order.append("first-releasing")

        async def second_task() -> None:
            # Small sleep so first starts first.
            await asyncio.sleep(0.02)
            async with second:
                order.append("second-acquired")

        # Act
        await asyncio.gather(first_task(), second_task())

        # Assert — second must not acquire until first releases.
        assert order == ["first-acquired", "first-releasing", "second-acquired"]

    async def test_times_out_when_lock_held_forever(self, tmp_path: Path) -> None:
        # Arrange
        target = tmp_path / "data.yaml"
        holder = FileLock(target, timeout=1.0)
        waiter = FileLock(target, timeout=0.1, retry_interval=0.01)
        await holder.acquire()
        try:
            # Act / Assert
            with pytest.raises(MembershipUnavailableError):
                await waiter.acquire()
        finally:
            holder.release()


class TestFileLockConstruction:
    async def test_non_positive_timeout_rejected(self, tmp_path: Path) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            FileLock(tmp_path / "x", timeout=0)
        with pytest.raises(ValueError):
            FileLock(tmp_path / "x", timeout=-1)

    async def test_non_positive_retry_interval_rejected(self, tmp_path: Path) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            FileLock(tmp_path / "x", timeout=1.0, retry_interval=0)


class TestAtomicWrite:
    async def test_round_trips_bytes(self, tmp_path: Path) -> None:
        # Arrange
        target = tmp_path / "out.bin"
        payload = bytes(range(256)) * 16

        # Act
        await atomic_write(target, payload)

        # Assert
        assert target.read_bytes() == payload

    async def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        # Arrange
        target = tmp_path / "out.bin"
        target.write_bytes(b"old")

        # Act
        await atomic_write(target, b"new-contents")

        # Assert
        assert target.read_bytes() == b"new-contents"

    async def test_creates_parent_directories(self, tmp_path: Path) -> None:
        # Arrange
        target = tmp_path / "nested" / "deep" / "out.bin"

        # Act
        await atomic_write(target, b"ok")

        # Assert
        assert target.read_bytes() == b"ok"

    async def test_does_not_leak_temp_file_on_success(self, tmp_path: Path) -> None:
        # Arrange
        target = tmp_path / "out.bin"

        # Act
        await atomic_write(target, b"hello")

        # Assert — only the target remains.
        contents = sorted(p.name for p in tmp_path.iterdir())
        assert contents == ["out.bin"]

    async def test_cleans_up_temp_file_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange — make os.replace raise so the temp file exists at cleanup time.
        target = tmp_path / "out.bin"
        real_replace = os.replace

        def boom(src: str, dst: str) -> None:  # pragma: no cover - helper
            del src, dst
            raise OSError("simulated rename failure")

        monkeypatch.setattr("pyleans.server.providers.file_lock.os.replace", boom)

        # Act / Assert
        with pytest.raises(OSError, match="simulated rename failure"):
            await atomic_write(target, b"won't land")

        # No temp files left in the directory.
        monkeypatch.setattr("pyleans.server.providers.file_lock.os.replace", real_replace)
        assert list(tmp_path.iterdir()) == []

    async def test_reader_never_sees_partial_content(self, tmp_path: Path) -> None:
        # Arrange
        target = tmp_path / "out.bin"
        await atomic_write(target, b"A" * 4096)
        stop = asyncio.Event()
        observed: list[int] = []

        async def reader() -> None:
            while not stop.is_set():
                if target.exists():
                    data = target.read_bytes()
                    observed.append(len(data))
                await asyncio.sleep(0)

        async def writer() -> None:
            for size in (4096, 16384, 4096, 32768, 4096):
                await atomic_write(target, b"X" * size)
                await asyncio.sleep(0)
            stop.set()

        # Act
        await asyncio.gather(reader(), writer())

        # Assert — every observed size is one of the known full sizes; no torn writes.
        assert set(observed) <= {4096, 16384, 32768}


class TestProviderIntegration:
    async def test_yaml_provider_writes_through_atomic_write(self, tmp_path: Path) -> None:
        # Arrange
        from pyleans.identity import SiloAddress, SiloInfo, SiloStatus
        from pyleans.server.providers.yaml_membership import YamlMembershipProvider

        provider = YamlMembershipProvider(str(tmp_path / "membership.yaml"))
        silo = SiloInfo(
            address=SiloAddress(host="h", port=1, epoch=1),
            status=SiloStatus.ACTIVE,
            last_heartbeat=1.0,
            start_time=1.0,
            cluster_id="dev",
        )

        # Act
        await provider.try_update_silo(silo)

        # Assert — temp files are cleaned; lock sidecar alongside target is allowed.
        names = {p.name for p in tmp_path.iterdir()}
        assert "membership.yaml" in names
        assert all(not n.endswith(".tmp") for n in names)


# ------ Cross-process integration ------------------------------------------

_CHILD_ACQUIRED_SENTINEL = "acquired"
_CHILD_RELEASED_SENTINEL = "released"


def _child_hold_lock(
    lock_path: str,
    hold_seconds: float,
    queue: mp.Queue[str],
) -> None:
    """Child process: acquire the lock, signal parent, hold, release."""

    async def run() -> None:
        lock = FileLock(Path(lock_path), timeout=5.0, retry_interval=0.01)
        async with lock:
            queue.put(_CHILD_ACQUIRED_SENTINEL)
            await asyncio.sleep(hold_seconds)
        queue.put(_CHILD_RELEASED_SENTINEL)

    asyncio.run(run())


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="mp spawn + fcntl interaction is flaky on CI Windows; "
    "POSIX integration run covers the lock semantics.",
)
class TestCrossProcessLock:
    async def test_two_processes_serialise_through_the_same_lock(self, tmp_path: Path) -> None:
        # Arrange — use "spawn" so the child inherits no state.
        ctx = mp.get_context("spawn")
        queue: mp.Queue[str] = ctx.Queue()
        lock_target = str(tmp_path / "cross.data")
        hold_seconds = 0.3
        child = ctx.Process(
            target=_child_hold_lock,
            args=(lock_target, hold_seconds, queue),
        )

        # Act
        child.start()
        try:
            # Wait for child to hold the lock.
            sentinel = await asyncio.to_thread(queue.get, True, 5.0)
            assert sentinel == _CHILD_ACQUIRED_SENTINEL

            # Parent attempts to acquire — must wait for child to release.
            parent_lock = FileLock(Path(lock_target), timeout=5.0, retry_interval=0.01)
            started = time.monotonic()
            await parent_lock.acquire()
            elapsed = time.monotonic() - started
            try:
                # Assert — parent waited at least until child released.
                released = await asyncio.to_thread(queue.get, True, 5.0)
                assert released == _CHILD_RELEASED_SENTINEL
                assert elapsed >= hold_seconds - 0.1
            finally:
                parent_lock.release()
        finally:
            child.join(timeout=5.0)
            assert child.exitcode == 0
