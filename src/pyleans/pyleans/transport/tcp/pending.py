"""Correlation-id to future registry with per-request timeout scheduling.

:class:`PendingRequests` is the core of request/response multiplexing on
a :class:`SiloConnection`. Every outbound request that expects a
response reserves a correlation id here; the read loop calls
:meth:`complete` or :meth:`fail` when the matching response arrives.

Correlation ids are monotonically increasing uint64 values allocated by
this table. Zero is reserved for messages that carry no correlation
(``ONE_WAY``) and never enters the pending table.

Late responses (arriving after a local timeout) are dropped silently —
see the class docstring for rationale.
"""

from __future__ import annotations

import asyncio
import logging

from pyleans.transport.errors import TransportTimeoutError

logger = logging.getLogger(__name__)

_UINT64_MAX = 2**64 - 1


class PendingRequests:
    """Correlation-id → :class:`asyncio.Future` registry.

    Single-threaded by construction: every public method runs inside one
    event loop, so no additional locking is required. The registry is
    *stateful* (next-id counter) and therefore one instance per
    :class:`SiloConnection`.

    Late-response handling: if a response arrives after the local
    timeout has fired, :meth:`complete` / :meth:`fail` silently drops
    it. Racing the local timeout against an in-flight response can
    otherwise produce :class:`asyncio.InvalidStateError` when both try
    to resolve the same future — gRPC and Orleans take the same stance.
    """

    def __init__(self, default_timeout: float) -> None:
        self._default_timeout = default_timeout
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[tuple[bytes, bytes]]] = {}
        self._timeouts: dict[int, asyncio.TimerHandle] = {}

    def __len__(self) -> int:
        return len(self._pending)

    def allocate(
        self, timeout: float | None = None
    ) -> tuple[int, asyncio.Future[tuple[bytes, bytes]]]:
        """Reserve a new correlation id and return ``(id, future)``.

        Arms a timer at ``timeout`` seconds (or the registry default if
        ``None``). When the timer fires before :meth:`complete` or
        :meth:`fail`, the future faults with
        :class:`TransportTimeoutError`. A non-positive timeout disables
        the timer; the caller must then resolve the future by other
        means (or wait forever).
        """
        loop = asyncio.get_running_loop()
        correlation_id = self._next_id
        self._advance_next_id()
        future: asyncio.Future[tuple[bytes, bytes]] = loop.create_future()
        self._pending[correlation_id] = future
        resolved = self._default_timeout if timeout is None else timeout
        if resolved > 0:
            handle = loop.call_later(resolved, self._expire, correlation_id)
            self._timeouts[correlation_id] = handle
        return correlation_id, future

    def complete(self, correlation_id: int, result: tuple[bytes, bytes]) -> None:
        """Resolve the future for ``correlation_id`` with ``result``.

        Silently dropped when the id is unknown or the future is already
        done. Cancels the timeout timer as part of the operation.
        """
        self._cancel_timeout(correlation_id)
        future = self._pending.pop(correlation_id, None)
        if future is None or future.done():
            return
        future.set_result(result)

    def fail(self, correlation_id: int, error: BaseException) -> None:
        """Fault the future for ``correlation_id`` with ``error``.

        Silently dropped when the id is unknown or the future is already
        done.
        """
        self._cancel_timeout(correlation_id)
        future = self._pending.pop(correlation_id, None)
        if future is None or future.done():
            return
        future.set_exception(error)

    def fail_all(self, error: BaseException) -> None:
        """Fault every outstanding future with ``error``.

        Used on disconnect: no outstanding request can succeed after
        the connection is gone, so the caller deserves a deterministic
        failure instead of a hang.
        """
        pending = self._pending
        timeouts = self._timeouts
        self._pending = {}
        self._timeouts = {}
        for handle in timeouts.values():
            handle.cancel()
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    def discard(self, correlation_id: int) -> None:
        """Drop an entry without resolving the future.

        Used when the caller has abandoned the await (cancellation, or
        a write failure before any response can arrive). No-op when the
        id is unknown.
        """
        self._cancel_timeout(correlation_id)
        self._pending.pop(correlation_id, None)

    def _expire(self, correlation_id: int) -> None:
        self._timeouts.pop(correlation_id, None)
        future = self._pending.pop(correlation_id, None)
        if future is None or future.done():
            return
        future.set_exception(TransportTimeoutError(f"request {correlation_id} timed out"))

    def _cancel_timeout(self, correlation_id: int) -> None:
        handle = self._timeouts.pop(correlation_id, None)
        if handle is not None:
            handle.cancel()

    def _advance_next_id(self) -> None:
        self._next_id += 1
        if self._next_id > _UINT64_MAX:
            self._next_id = 1
