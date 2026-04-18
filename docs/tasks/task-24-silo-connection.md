# Task 24: `SiloConnection` -- Single Multiplexed Connection With Correlation and Backpressure

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-22-transport-abcs.md](task-22-transport-abcs.md)
- [task-23-wire-protocol.md](task-23-wire-protocol.md)

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
    msg = await read_frame(reader, max_size)      # from task-23
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

- [ ] `send_request` returns the response header/body on success
- [ ] `send_request` raises `TransportTimeoutError` when the peer never responds
- [ ] `send_request` raises `TransportConnectionError` when the connection is lost mid-request
- [ ] `send_request` raises `TransportClosedError` after `close()`
- [ ] In-flight semaphore bounds concurrent pending requests; N+1'th caller blocks until a permit frees
- [ ] Requests and responses multiplex correctly: 100 concurrent `send_request` calls all complete in interleaved order
- [ ] Write lock prevents byte-level frame interleaving (test by injecting a slow `drain` and verifying frame boundaries remain intact)
- [ ] Late response after timeout is dropped without error
- [ ] Keepalive ping fires when idle longer than `keepalive_interval`; missing PONG triggers `close("lost")`
- [ ] Inbound REQUEST whose handler raises is converted to `MessageType.ERROR` response; connection stays open
- [ ] Malformed frame closes the connection and fails all pending futures
- [ ] Graceful `close("normal")` drains in-flight requests up to `close_drain_timeout`
- [ ] Unit tests use an in-memory `asyncio.StreamReader`/`StreamWriter` pair (`asyncio.StreamReaderProtocol` or a memory-transport helper) so no real sockets are needed

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
