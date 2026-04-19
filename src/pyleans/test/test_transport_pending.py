"""Tests for :class:`pyleans.transport.tcp.pending.PendingRequests`."""

from __future__ import annotations

import asyncio

import pytest
from pyleans.transport.errors import TransportConnectionError, TransportTimeoutError
from pyleans.transport.tcp.pending import PendingRequests


class TestAllocate:
    async def test_allocates_monotonically_increasing_ids(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=1.0)

        # Act
        id_a, _ = pending.allocate(timeout=0)
        id_b, _ = pending.allocate(timeout=0)
        id_c, _ = pending.allocate(timeout=0)

        # Assert
        assert id_a == 1
        assert id_b == 2
        assert id_c == 3

    async def test_never_allocates_zero(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)

        # Act
        allocated_ids = [pending.allocate(timeout=0)[0] for _ in range(5)]

        # Assert
        assert 0 not in allocated_ids

    async def test_allocate_returns_an_unresolved_future(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)

        # Act
        _, future = pending.allocate(timeout=0)

        # Assert
        assert not future.done()


class TestComplete:
    async def test_complete_resolves_future_with_result(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)
        correlation_id, future = pending.allocate(timeout=0)

        # Act
        pending.complete(correlation_id, (b"hdr", b"body"))

        # Assert
        assert future.result() == (b"hdr", b"body")

    async def test_complete_unknown_id_is_silently_dropped(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)

        # Act / Assert — must not raise
        pending.complete(correlation_id=9999, result=(b"", b""))

    async def test_late_response_after_timeout_is_silently_dropped(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)
        correlation_id, future = pending.allocate(timeout=0.01)

        # Act
        with pytest.raises(TransportTimeoutError):
            await future
        pending.complete(correlation_id, (b"late", b"response"))

        # Assert — future already resolved with timeout; second complete is a no-op
        assert isinstance(future.exception(), TransportTimeoutError)

    async def test_complete_removes_entry(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)
        correlation_id, _ = pending.allocate(timeout=0)
        assert len(pending) == 1

        # Act
        pending.complete(correlation_id, (b"", b""))

        # Assert
        assert len(pending) == 0


class TestFail:
    async def test_fail_resolves_future_with_exception(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)
        correlation_id, future = pending.allocate(timeout=0)
        error = TransportConnectionError("lost")

        # Act
        pending.fail(correlation_id, error)

        # Assert
        assert future.exception() is error

    async def test_fail_unknown_id_is_silently_dropped(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)

        # Act / Assert — must not raise
        pending.fail(correlation_id=9999, error=RuntimeError("boom"))


class TestFailAll:
    async def test_fail_all_faults_every_outstanding_future(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)
        futures = [pending.allocate(timeout=0)[1] for _ in range(3)]

        # Act
        pending.fail_all(TransportConnectionError("bye"))

        # Assert
        for future in futures:
            assert isinstance(future.exception(), TransportConnectionError)
        assert len(pending) == 0

    async def test_fail_all_cancels_pending_timers(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)
        _, future = pending.allocate(timeout=0.05)

        # Act
        pending.fail_all(TransportConnectionError("bye"))
        await asyncio.sleep(0.1)  # wait past the original timeout

        # Assert — future still bears the fail_all exception, not the timeout
        exc = future.exception()
        assert isinstance(exc, TransportConnectionError)
        assert str(exc) == "bye"


class TestTimeout:
    async def test_timeout_faults_future_with_transport_timeout_error(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)
        _, future = pending.allocate(timeout=0.01)

        # Act
        with pytest.raises(TransportTimeoutError):
            await future

        # Assert
        assert len(pending) == 0

    async def test_default_timeout_applied_when_timeout_is_none(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0.01)
        _, future = pending.allocate(timeout=None)

        # Act / Assert
        with pytest.raises(TransportTimeoutError):
            await future

    async def test_non_positive_timeout_disables_timer(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)
        _, future = pending.allocate(timeout=0)

        # Act
        await asyncio.sleep(0.05)

        # Assert — future still pending; no timer was armed
        assert not future.done()


class TestDiscard:
    async def test_discard_removes_entry_without_resolving_future(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)
        correlation_id, future = pending.allocate(timeout=0)

        # Act
        pending.discard(correlation_id)

        # Assert
        assert len(pending) == 0
        assert not future.done()

    async def test_discard_unknown_id_is_noop(self) -> None:
        # Arrange
        pending = PendingRequests(default_timeout=0)

        # Act / Assert
        pending.discard(correlation_id=9999)
