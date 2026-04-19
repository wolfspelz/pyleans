# Task 02-06: `SiloConnection` -- Single Multiplexed Connection With Correlation and Backpressure

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-04-transport-abcs.md](task-02-04-transport-abcs.md)
- [task-02-05-wire-protocol.md](task-02-05-wire-protocol.md)

## References
- [architecture/pyleans-transport.md](../architecture/pyleans-transport.md) -- §4.4 correlation, §4.5 multiplexing, §4.6 keepalive, §4.8 backpressure
- [orleans-networking.md](../orleans-architecture/orleans-networking.md) -- §8 multiplexing, §9 correlation
- Research inputs: Orleans `ConnectionCommon` / `ConnectionManager`; Hashicorp memberlist; gRPC HTTP/2 stream management; Python `asyncio` docs on backpressure idioms (`writer.drain()`, `get_write_buffer_size()`)

## Description

`SiloConnection` wraps one authenticated TCP connection between two silos and gives the layer above a request/response API. This is where the reliability guarantees land: in-order delivery, bounded pending requests, per-request timeouts, fail-fast on disconnect, and graceful close. Every reliability invariant the cluster depends on either lives here or cannot be added above.

A single `SiloConnection` instance owns:
- the `StreamReader`/`StreamWriter` pair,
- one read-loop coroutine that drains frames and dispatches,
- one write lock that serializes `writer.write()` calls so frames never interleave at the byte level,
- a pending-request table keyed by correlation id with per-request timeout scheduling,
- an in-flight semaphore that caps memory usage under load,
- a keepalive coroutine that sends PING/PONG when the connection is idle.

### Files to create

- `src/pyleans/pyleans/transport/tcp/connection.py`
- `src/pyleans/pyleans/transport/tcp/pending.py` -- `PendingRequests` table

### Design

```python
# pending.py
class PendingRequests:
    """Correlation-id -> asyncio.Future registry with per-request timeouts.

    Correlation ids are monotonically increasing uint64, allocated by this
    table. Zero is reserved for one-way messages (never enters the table).
    """

    def __init__(self, default_timeout: float) -> None: ...

    def allocate(self, timeout: float | None) -> tuple[int, asyncio.Future]:
        """Reserve a new correlation id and return (id, future).
        Timer is armed; if no response arrives by deadline, the future faults
        with TransportTimeoutError.
        """

    def complete(self, correlation_id: int, result: tuple[bytes, bytes]) -> None:
        """Deliver a response. Silently dropped if id unknown or future already
        done (late responses after timeout are not an error)."""

    def fail(self, correlation_id: int, error: BaseException) -> None: ...

    def fail_all(self, error: BaseException) -> None:
        """Called on disconnect: every outstanding future is faulted."""
```

Late-response drops are deliberate. Under pathological conditions a request can time out locally milliseconds before the response arrives; faulting the caller on timeout and then *also* trying to complete the future would raise `InvalidStateError`. Dropping silently matches gRPC and Orleans behavior.

```python
# connection.py
class SiloConnection:
    """One authenticated, multiplexed connection to a remote silo."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        remote_silo: SiloAddress,
        options: TransportOptions,
        message_handler: MessageHandler,
    ) -> None: ...

    async def start(self) -> None:
        """Launch read_loop and keepalive coroutines. Safe to call once."""

    async def close(self, reason: str) -> None:
        """Graceful close:
          1. Stop accepting new sends (mark closed).
          2. Wait up to options.close_drain_timeout for in-flight requests.
          3. Cancel read_loop + keepalive.
          4. Close writer, drain, wait_closed.
          5. Fail any still-outstanding futures with TransportConnectionError.
        Idempotent.
        """

    async def send_request(
        self, header: bytes, body: bytes, timeout: float | None = None
    ) -> tuple[bytes, bytes]: ...

    async def send_one_way(self, header: bytes, body: bytes) -> None: ...

    async def send_ping(self, timeout: float) -> float: ...
```

### Read loop -- the single reader

Exactly one coroutine reads from the socket. Every inbound frame is dispatched by `message_type`:

```
while not closed:
    msg = await read_frame(reader, max_size)      # from task-02-05
    match msg.message_type:
        RESPONSE | ERROR -> pending.complete/fail(msg.correlation_id, ...)
        REQUEST          -> asyncio.create_task(_handle_request(msg))
        ONE_WAY          -> asyncio.create_task(_handle_one_way(msg))
        PING             -> schedule PONG with same correlation_id
        PONG             -> pending.complete(msg.correlation_id, None)
```

`_handle_request` awaits `message_handler(remote_silo, msg)`, catches every exception, and turns application exceptions into a `MessageType.ERROR` response. It never re-raises into the read loop, so a buggy handler cannot kill the connection.

Dispatching inbound REQUEST/ONE_WAY as new tasks (not inline) preserves the multiplex invariant: a slow handler for one request doesn't block the next frame from being read. Backpressure on the *inbound* side comes from TCP window + a bounded `asyncio.Semaphore` guarding how many handler tasks run concurrently (`options.max_inbound_concurrency`, default 256).

### Write lock

All writes funnel through:

```python
async with self._write_lock:
    self._writer.write(frame)
    await self._writer.drain()
```

`asyncio.Lock` is used (not just relying on the single-threaded event loop) because `drain()` yields, and a second `write()` before the first `drain()` would interleave frame bytes on the wire. This is the single mistake that turns "works under test" into "mystery framing errors under load."

### Backpressure -- three layers, in order

1. **In-flight semaphore** (`options.max_in_flight_requests`): `send_request` acquires a permit before allocating a correlation id. When the permit pool is empty, callers `await` in fair order. This bounds memory (pending-request table size) and pending futures.
2. **Write-buffer high-water mark**: after each write, check `writer.transport.get_write_buffer_size()`. If above the high-water mark (1 MB default), refuse new sends with `BackpressureError` or yield until it drains -- configurable via `options.backpressure_mode` (`"block"` default, `"raise"` opt-in).
3. **TCP flow control**: `writer.drain()` yields when the kernel buffer is full, propagating backpressure all the way to the sender's socket.

All three are needed. Semaphore alone doesn't bound the OS-buffered bytes; drain alone doesn't bound the pending-request table; HWM alone doesn't bound pending responses.

### Keepalive

One coroutine sleeps `options.keepalive_interval`, checks "was there any activity in this window", and issues a `send_ping(timeout=options.keepalive_timeout)` if not. Failed ping -> transition to "lost" state -> fail all pending futures -> close. Activity counter is bumped by the read loop on every frame received and by `send_*` on every frame sent.

### Close semantics -- graceful vs abrupt

`close("normal")`:
- Mark closed; new `send_*` calls raise `TransportClosedError`.
- Wait `options.close_drain_timeout` (default 5 s) for outstanding `send_request` futures to complete normally.
- After timeout, fail them with `TransportConnectionError("connection closing")`.
- Cancel read_loop / keepalive, close writer, `await writer.wait_closed()`.

`close("lost")`:
- Same shape but skips the drain wait -- fail everything immediately with `TransportConnectionError`.

Separating the two paths means normal shutdown never loses responses, while disconnect-driven close doesn't block shutdown on futures that will never complete.

### Handling exceptions in the read loop

Every exception in the read loop terminates the connection. Classification:

| Exception | Action | Rationale |
|---|---|---|
| `asyncio.IncompleteReadError` / `ConnectionError` | `close("lost")` | Peer gone. |
| `MessageTooLargeError` | `close("lost")` | Protocol violation; cannot resync mid-stream. |
| `ValueError` (malformed frame) | `close("lost")` | Same as above -- desync is unrecoverable over a stream. |
| `asyncio.CancelledError` | propagate | Clean shutdown path. |
| Any other exception | log at ERROR, `close("lost")` | Fail-safe default. |

### Acceptance criteria

- [x] `send_request` returns the response header/body on success
- [x] `send_request` raises `TransportTimeoutError` when the peer never responds
- [x] `send_request` raises `TransportConnectionError` when the connection is lost mid-request
- [x] `send_request` raises `TransportClosedError` after `close()`
- [x] In-flight semaphore bounds concurrent pending requests; N+1'th caller blocks until a permit frees
- [x] Requests and responses multiplex correctly: 100 concurrent `send_request` calls all complete in interleaved order
- [x] Write lock prevents byte-level frame interleaving (test by injecting a slow `drain` and verifying frame boundaries remain intact)
- [x] Late response after timeout is dropped without error
- [x] Keepalive ping fires when idle longer than `keepalive_interval`; missing PONG triggers `close("lost")`
- [x] Inbound REQUEST whose handler raises is converted to `MessageType.ERROR` response; connection stays open
- [x] Malformed frame closes the connection and fails all pending futures
- [x] Graceful `close("normal")` drains in-flight requests up to `close_drain_timeout`
- [x] Unit tests use the `InMemoryNetwork` from [task-01-16](task-01-16-in-memory-network-simulator.md) -- `SiloConnection` receives `StreamReader`/`StreamWriter` pairs from `InMemoryNetwork.open_connection` / `start_server`, so no OS-level sockets are bound and failure modes (reset, peer-close, backpressure) can be exercised deterministically via the simulator's injection hooks

## Findings of code review

- **[resolved] `messages.py` `TransportMessage` docstring claimed PING/PONG always carry `correlation_id=0`.** The task-02-06 keepalive design matches PONGs against their PINGs by correlation id, so the claim is incompatible with the connection-layer design. Fixed: docstring updated to state that PING/PONG carry a non-zero id (the same id on both sides of the exchange), while ONE_WAY remains 0.
- **[resolved] `try/except: raise` of `asyncio.CancelledError` in the read loop, handler wrapper, one-way handler, and keepalive loop were no-ops under Python 3.12 (CancelledError inherits from BaseException, so the generic `except Exception` below would not swallow it anyway) and tripped pylint `W0706`.** Removed the bare re-raises.
- **[resolved] `options.TransportOptions` was missing the connection-level knobs the task spec names.** Added `close_drain_timeout`, `max_inbound_concurrency`, `write_buffer_high_water`, and `backpressure_mode` (`Literal["block", "raise"]`) with defaults matching the spec (5 s / 256 / 1 MiB / `"block"`). Existing tests for the dataclass remained green.
- **[resolved] `create_task(self.close("lost"))` references discarded** — tripped `RUF006` and risked the task being GC'd before the close finished. Added a `_close_tasks` set plus `_schedule_close_lost()` helper that stores the task and discards on completion.
- **[noted] PendingRequests uses a monotonic counter that wraps at `uint64_max`.** After 2⁶⁴ allocations a collision with a long-running request becomes theoretically possible. At 10⁶ req/s this is ~584 000 years; left as-is.

## Findings of security review

- **[resolved] Inbound handler tasks were spawned before acquiring `max_inbound_concurrency`.** Under a hostile sender this lets a peer flood the read loop; each extra REQUEST/ONE_WAY frame allocates a new `asyncio.Task` blocked on the semaphore, so memory grows unbounded with frame rate even though only N handlers run concurrently. Fixed: the read loop now `await`s the inbound semaphore **before** creating the handler task. When the cap is reached the read loop pauses, the socket's read buffer fills, and the TCP window closes — the spec's stated backpressure chain is now actually wired up. Release is deferred to the task's done-callback. Tests confirm the bound holds end-to-end (`TestInFlightSemaphore::test_semaphore_bounds_concurrent_pending_requests`, adapted to exercise the inbound path).
- **[reviewed, no change] OWASP Top 10 against this code.** No SQL/OS/HTML injection surfaces (bytes are opaque at this layer). No file I/O or path handling. No SSRF surface. Auth is the handshake's job (task-02-04/02-08), not this module's. Logging never emits header/body contents.
- **[reviewed, no change] Unbounded-resource checklist.**
  - Pending-request table bounded by `max_in_flight_requests` (semaphore acquired before allocating a correlation id).
  - Concurrent handler tasks bounded by `max_inbound_concurrency` (fixed above).
  - Outbound write buffer bounded by `backpressure_mode="block"` (drain-yielding) or `"raise"` (explicit `BackpressureError` above HWM).
  - Frame size bounded by `max_message_size`, enforced by the task-02-05 wire codec before any payload is allocated.
- **[reviewed, no change] Late-response race.** `PendingRequests.complete` / `fail` silently drop duplicates / late arrivals rather than raising `InvalidStateError`, matching gRPC / Orleans behaviour. This is intentional per the task spec.
- **[reviewed, no change] Close-on-write-failure path.** When `writer.write` / `writer.drain` raises, `_schedule_close_lost("write-err")` fires and the caller sees `TransportConnectionError`. The scheduled close fails every pending future and tears down the writer, so a single failed write fails fast rather than leaking pending requests.
- **[reviewed, no change] Secret-logging rule (CLAUDE.md).** No log line includes `header` or `body` payload bytes; only sizes, correlation ids, remote silo identity, and state transitions.

## Summary of implementation

### Files created

- [src/pyleans/pyleans/transport/tcp/__init__.py](../../src/pyleans/pyleans/transport/tcp/__init__.py) -- package init exporting `SiloConnection` and `PendingRequests`.
- [src/pyleans/pyleans/transport/tcp/pending.py](../../src/pyleans/pyleans/transport/tcp/pending.py) -- `PendingRequests` correlation table with per-request timeout scheduling, `complete` / `fail` / `fail_all` / `discard`, and silent drop of late arrivals.
- [src/pyleans/pyleans/transport/tcp/connection.py](../../src/pyleans/pyleans/transport/tcp/connection.py) -- `SiloConnection` with read loop, keepalive loop, write lock, in-flight semaphore, inbound-concurrency semaphore, backpressure enforcement, graceful / lost close paths, and `_schedule_close_lost` self-close helper.
- [src/pyleans/test/test_transport_pending.py](../../src/pyleans/test/test_transport_pending.py) -- 16 unit tests exercising `PendingRequests` (happy path, ids never zero, timer expiry, late-response drop, `fail_all` cancels timers, discard, default timeout, non-positive timeout disables timer).
- [src/pyleans/test/test_transport_connection.py](../../src/pyleans/test/test_transport_connection.py) -- 18 connection tests wired over `InMemoryNetwork` covering every acceptance bullet: request/response, timeout, multiplexing, in-flight cap, write-lock frame integrity, connection loss, closed-state behaviour, graceful drain, handler-raise → ERROR response, inbound ERROR propagation, malformed frame, keepalive PING, missing-PONG → close("lost"), `backpressure_mode="raise"`, `ONE_WAY`.

### Files modified

- [src/pyleans/pyleans/transport/options.py](../../src/pyleans/pyleans/transport/options.py) -- added `close_drain_timeout`, `max_inbound_concurrency`, `write_buffer_high_water`, `backpressure_mode` fields plus matching `DEFAULT_*` constants and a `BackpressureMode` `Literal` alias. No existing field or default changed.
- [src/pyleans/pyleans/transport/messages.py](../../src/pyleans/pyleans/transport/messages.py) -- corrected the `TransportMessage.correlation_id` docstring to reflect that PING/PONG carry a non-zero id (required by keepalive RTT matching); ONE_WAY remains 0.

### Key implementation decisions

- **Inbound semaphore acquired in the read loop** rather than inside the handler task. The task spec says backpressure comes from "TCP window + a bounded semaphore"; the TCP window only closes if the read loop actually stops reading, which requires the acquire to happen before `create_task`. Acquiring inside the task would spawn unbounded waiting tasks. See the security-review finding for the full argument.
- **PING/PONG carry non-zero correlation ids**, allocated via `PendingRequests.allocate`, so `send_ping` can measure RTT deterministically and the read loop can resolve the right PING future when the PONG arrives. The messages-module docstring was updated to match.
- **Two-step close.** Enter `"closing"` under `_close_lock` (fast, sync transition), then do the slow work (drain, cancel tasks, close writer, await read task, cancel handler tasks) outside the lock. A second `close()` sees `"closing"` / `"closed"` and returns immediately, keeping `close()` idempotent without holding the lock for seconds.
- **`_schedule_close_lost` helper** centralises the fire-and-forget self-close used by the read loop, the write path, and the keepalive loop. The created task is stored in `_close_tasks` so asyncio never warns about a GC'd pending task.
- **Silent-drop on duplicate / late arrivals.** `PendingRequests.complete` and `fail` return quietly when the correlation id is unknown or the future is already done, rather than raising `InvalidStateError`. This matches gRPC and Orleans and is explicitly called out in the task spec.

### Deviations from the original design

None substantive. The task spec's `_spawn_handler_task` was inverted (semaphore acquired before spawn, not inside the handler) to make the stated TCP-window backpressure actually engage; the observable behaviour -- a bounded number of concurrent handlers -- matches the spec.

### Test coverage summary

- 16 `PendingRequests` tests + 18 `SiloConnection` tests = **34 new tests, all passing.** Each test is AAA-labelled with exactly one Act.
- Full suite: **606 passed** (up from 572 before the task).
- `ruff check` + `ruff format --check` clean.
- `pylint src/pyleans/pyleans src/counter_app src/counter_client` rated **10.00/10**.
- `mypy` reports no new errors in any file touched by this task; the 24 pre-existing errors in unrelated test modules are unchanged.
