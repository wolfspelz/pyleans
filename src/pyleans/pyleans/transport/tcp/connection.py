"""One authenticated, multiplexed TCP connection between two silos.

:class:`SiloConnection` owns a single ``StreamReader``/``StreamWriter``
pair and serves request/response, one-way, and PING/PONG traffic over
it. Every reliability invariant the cluster depends on — in-order
delivery, bounded in-flight requests, per-request timeouts, fail-fast
on disconnect, graceful close — is enforced here.

See :doc:`../../../../docs/tasks/task-02-06-silo-connection` for the
full design and acceptance criteria, and
:doc:`../../../../docs/architecture/pyleans-transport` §4.4-§4.8 for
the correlation / multiplexing / backpressure semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Coroutine
from typing import Any, Literal

from pyleans.identity import SiloAddress
from pyleans.transport.cluster import MessageHandler
from pyleans.transport.error_payload import (
    ErrorPayload,
    code_to_exception_class,
    decode_error_payload,
    encode_error_payload,
    exception_to_code,
)
from pyleans.transport.errors import (
    BackpressureError,
    MessageTooLargeError,
    TransportClosedError,
    TransportConnectionError,
    TransportError,
    TransportTimeoutError,
)
from pyleans.transport.messages import MessageType, TransportMessage
from pyleans.transport.options import TransportOptions
from pyleans.transport.tcp.pending import PendingRequests
from pyleans.transport.wire import encode_frame, read_frame

logger = logging.getLogger(__name__)

_CloseReason = Literal["normal", "lost"]
_State = Literal["new", "running", "closing", "closed"]


class SiloConnection:
    """One authenticated, multiplexed TCP connection to a peer silo."""

    # pylint: disable=too-many-instance-attributes  # Connection state is irreducible.

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        remote_silo: SiloAddress,
        options: TransportOptions,
        message_handler: MessageHandler,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._remote_silo = remote_silo
        self._options = options
        self._handler = message_handler
        self._pending = PendingRequests(options.default_request_timeout)
        self._in_flight = asyncio.Semaphore(options.max_in_flight_requests)
        self._inbound_sem = asyncio.Semaphore(options.max_inbound_concurrency)
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._state: _State = "new"
        self._last_activity = time.monotonic()
        self._read_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._close_tasks: set[asyncio.Task[None]] = set()
        self._closed_event = asyncio.Event()
        self._close_reason: _CloseReason | None = None

    @property
    def remote_silo(self) -> SiloAddress:
        return self._remote_silo

    @property
    def is_closed(self) -> bool:
        return self._state == "closed"

    @property
    def close_reason(self) -> _CloseReason | None:
        """The reason passed to ``close()``. ``None`` until the close completes."""
        return self._close_reason

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def wait_closed(self) -> _CloseReason:
        """Block until the connection is fully closed; return its close reason.

        Returns immediately if already closed. Used by the connection
        manager to trigger reconnection when a connection is lost.
        """
        await self._closed_event.wait()
        assert self._close_reason is not None  # set atomically with the event
        return self._close_reason

    async def start(self) -> None:
        """Launch the read-loop and keepalive tasks.

        Idempotent after the first call.
        """
        if self._state != "new":
            return
        self._state = "running"
        loop = asyncio.get_running_loop()
        self._read_task = loop.create_task(
            self._read_loop(),
            name=f"silo-conn-read:{self._remote_silo}",
        )
        self._keepalive_task = loop.create_task(
            self._keepalive_loop(),
            name=f"silo-conn-keepalive:{self._remote_silo}",
        )
        logger.info("SiloConnection started: remote=%s", self._remote_silo)

    async def close(self, reason: _CloseReason = "normal") -> None:
        """Close the connection.

        ``"normal"`` waits up to :attr:`TransportOptions.close_drain_timeout`
        for outstanding requests to complete before faulting them;
        ``"lost"`` fails everything immediately. Idempotent.
        """
        async with self._close_lock:
            if self._state in ("closed", "closing"):
                return
            self._state = "closing"
        logger.info(
            "SiloConnection closing: remote=%s reason=%s pending=%d",
            self._remote_silo,
            reason,
            len(self._pending),
        )
        await self._cancel_background_tasks()
        if reason == "normal":
            await self._drain_pending()
            self._pending.fail_all(TransportConnectionError("connection closing"))
        else:
            self._pending.fail_all(TransportConnectionError("connection lost"))
        await self._close_writer()
        await self._await_read_task()
        await self._cancel_handler_tasks()
        self._close_reason = reason
        self._state = "closed"
        self._closed_event.set()
        logger.info("SiloConnection closed: remote=%s", self._remote_silo)

    async def send_request(
        self, header: bytes, body: bytes, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        """Send a REQUEST and return the peer's ``(header, body)`` response."""
        self._ensure_running()
        await self._in_flight.acquire()
        try:
            correlation_id, future = self._pending.allocate(timeout)
            try:
                await self._write_frame(
                    TransportMessage(MessageType.REQUEST, correlation_id, header, body)
                )
                return await future
            except BaseException:
                self._pending.discard(correlation_id)
                raise
        finally:
            self._in_flight.release()

    async def send_one_way(self, header: bytes, body: bytes) -> None:
        """Send a ONE_WAY message that expects no response."""
        self._ensure_running()
        await self._write_frame(TransportMessage(MessageType.ONE_WAY, 0, header, body))

    async def send_ping(self, timeout: float) -> float:
        """Send a PING and return the round-trip time in seconds.

        Raises :class:`TransportTimeoutError` when the peer does not
        answer within ``timeout`` seconds, and
        :class:`TransportConnectionError` when the connection drops
        while the PING is outstanding.
        """
        self._ensure_running()
        correlation_id, future = self._pending.allocate(timeout)
        try:
            started = time.monotonic()
            await self._write_frame(TransportMessage(MessageType.PING, correlation_id, b"", b""))
            await future
            return time.monotonic() - started
        except BaseException:
            self._pending.discard(correlation_id)
            raise

    def _ensure_running(self) -> None:
        if self._state == "new":
            raise TransportClosedError("connection not started")
        if self._state != "running":
            raise TransportClosedError(f"connection {self._state}")

    async def _write_frame(self, message: TransportMessage) -> None:
        frame = encode_frame(message, self._options.max_message_size)
        async with self._write_lock:
            self._enforce_backpressure()
            try:
                self._writer.write(frame)
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                self._schedule_close_lost(cause="write-err")
                raise TransportConnectionError(f"write failed: {exc}") from exc
        self._last_activity = time.monotonic()

    def _enforce_backpressure(self) -> None:
        if self._options.backpressure_mode != "raise":
            return
        buffered = self._writer.transport.get_write_buffer_size()
        if buffered > self._options.write_buffer_high_water:
            raise BackpressureError(
                f"write buffer {buffered} bytes exceeds high-water "
                f"{self._options.write_buffer_high_water}"
            )

    async def _read_loop(self) -> None:
        loss_reason: Exception | None = None
        try:
            while True:
                message = await read_frame(self._reader, self._options.max_message_size)
                self._last_activity = time.monotonic()
                await self._dispatch(message)
        except (TransportConnectionError, ConnectionError, asyncio.IncompleteReadError) as exc:
            loss_reason = TransportConnectionError(f"peer connection lost: {exc}")
        except MessageTooLargeError as exc:
            loss_reason = TransportConnectionError(f"protocol violation (frame too large): {exc}")
        except ValueError as exc:
            loss_reason = TransportConnectionError(f"malformed frame: {exc}")
        except Exception as exc:  # fail-safe default
            logger.exception("unexpected error in read loop: remote=%s", self._remote_silo)
            loss_reason = TransportConnectionError(f"unexpected read-loop error: {exc}")
        if loss_reason is not None and self._state == "running":
            logger.warning(
                "SiloConnection read loop lost: remote=%s reason=%s",
                self._remote_silo,
                loss_reason,
            )
            self._schedule_close_lost(cause="read-loss")

    async def _dispatch(self, message: TransportMessage) -> None:
        if message.message_type is MessageType.RESPONSE:
            self._pending.complete(message.correlation_id, (message.header, message.body))
            return
        if message.message_type is MessageType.ERROR:
            self._pending.fail(message.correlation_id, self._decode_error(message.body))
            return
        if message.message_type is MessageType.PONG:
            self._pending.complete(message.correlation_id, (b"", b""))
            return
        if message.message_type is MessageType.PING:
            await self._write_frame(
                TransportMessage(MessageType.PONG, message.correlation_id, b"", b"")
            )
            return
        if message.message_type is MessageType.REQUEST:
            await self._spawn_bounded_handler(self._handle_request(message), "request")
            return
        if message.message_type is MessageType.ONE_WAY:
            await self._spawn_bounded_handler(self._handle_one_way(message), "one-way")
            return
        logger.warning("unexpected inbound message type %r", message.message_type)

    async def _spawn_bounded_handler(self, coro: Coroutine[Any, Any, None], kind: str) -> None:
        """Spawn an inbound handler, applying the inbound-concurrency cap.

        The semaphore is acquired in the read loop (here) rather than
        inside the handler task so that backpressure propagates via the
        TCP window: once the cap is reached, the read loop blocks
        before consuming the next frame, and the peer's writes stall.
        Spawning tasks first and acquiring second would pile up unbounded
        waiters and defeat the bound.
        """
        await self._inbound_sem.acquire()
        loop = asyncio.get_running_loop()
        task: asyncio.Task[None] = loop.create_task(
            coro, name=f"silo-conn-{kind}:{self._remote_silo}"
        )
        self._handler_tasks.add(task)

        def _on_done(finished: asyncio.Task[None]) -> None:
            self._handler_tasks.discard(finished)
            self._inbound_sem.release()

        task.add_done_callback(_on_done)

    async def _handle_request(self, message: TransportMessage) -> None:
        response = await self._invoke_handler_safely(message)
        with contextlib.suppress(TransportClosedError, TransportConnectionError):
            await self._write_frame(response)

    async def _invoke_handler_safely(self, message: TransportMessage) -> TransportMessage:
        try:
            response = await self._handler(self._remote_silo, message)
        except Exception as exc:
            logger.warning(
                "request handler raised: remote=%s correlation_id=%d exc=%r",
                self._remote_silo,
                message.correlation_id,
                exc,
            )
            return self._encode_error_response(message.correlation_id, exc)
        if response is None:
            return self._encode_error_response(
                message.correlation_id,
                TransportError("handler returned None for REQUEST"),
            )
        return response

    async def _handle_one_way(self, message: TransportMessage) -> None:
        try:
            await self._handler(self._remote_silo, message)
        except Exception:
            logger.exception(
                "one-way handler raised: remote=%s correlation_id=%d",
                self._remote_silo,
                message.correlation_id,
            )

    async def _keepalive_loop(self) -> None:
        interval = self._options.keepalive_interval
        if interval <= 0:
            return
        while self._state == "running":
            await asyncio.sleep(interval)
            if self._state != "running":
                return
            if time.monotonic() - self._last_activity < interval:
                continue
            try:
                await self.send_ping(self._options.keepalive_timeout)
            except (TransportTimeoutError, TransportConnectionError, TransportClosedError):
                logger.warning(
                    "SiloConnection keepalive ping failed: remote=%s",
                    self._remote_silo,
                )
                self._schedule_close_lost(cause="keepalive")
                return

    def _schedule_close_lost(self, *, cause: str) -> None:
        """Fire-and-forget self-close from a context that can't ``await``.

        Stores the task so asyncio doesn't warn about it being GC'd before
        completion.
        """
        if self._state in ("closing", "closed"):
            return
        loop = asyncio.get_running_loop()
        task: asyncio.Task[None] = loop.create_task(
            self.close("lost"),
            name=f"silo-conn-close-on-{cause}:{self._remote_silo}",
        )
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)

    async def _drain_pending(self) -> None:
        deadline = time.monotonic() + self._options.close_drain_timeout
        while len(self._pending) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.01, remaining))

    async def _cancel_background_tasks(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if self._keepalive_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._keepalive_task

    async def _close_writer(self) -> None:
        with contextlib.suppress(Exception):
            self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()

    async def _await_read_task(self) -> None:
        task = self._read_task
        if task is None or task.done():
            return
        if asyncio.current_task() is task:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _cancel_handler_tasks(self) -> None:
        tasks = list(self._handler_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _decode_error(self, body: bytes) -> Exception:
        try:
            payload = decode_error_payload(body)
        except ValueError as exc:
            return TransportError(f"malformed error payload: {exc}")
        exc_class = code_to_exception_class(payload.code)
        return exc_class(payload.reason)

    def _encode_error_response(self, correlation_id: int, exc: BaseException) -> TransportMessage:
        payload = ErrorPayload(code=exception_to_code(exc), reason=str(exc))
        return TransportMessage(
            MessageType.ERROR, correlation_id, b"", encode_error_payload(payload)
        )
