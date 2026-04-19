# Task 02-07: `SiloConnectionManager` -- Full-Mesh Manager With Reconnection and Deduplication

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-05-wire-protocol.md](task-02-05-wire-protocol.md)
- [task-02-06-silo-connection.md](task-02-06-silo-connection.md)

## References
- [architecture/pyleans-transport.md](../architecture/pyleans-transport.md) -- §4.1 handshake and dedup, §4.2 full mesh topology, §4.7 reconnection strategy
- [orleans-networking.md](../orleans-architecture/orleans-networking.md) -- §6 silo-to-silo connection management
- Research: Hashicorp memberlist reconnection, gRPC connection backoff (gRPC A6), Akka remoting reconnect policy

## Description

Above a single `SiloConnection` sits the manager that owns **all** connections from this silo to every other silo in the cluster. Its job:

1. Establish and maintain one connection per remote silo (full mesh).
2. Deduplicate concurrent connect attempts (both sides dialing at once).
3. Perform the handshake and register completed connections.
4. Reconnect lost connections with exponential backoff + jitter while the peer remains in active membership.
5. Fire connection-established / connection-lost callbacks for the directory cache and failure detector.
6. Accept inbound connections from `asyncio.start_server` (driven by [task-02-08](task-02-08-tcp-cluster-transport.md)).

This is where the reliability story stops being about bytes and starts being about *which peer am I connected to, and what if I'm not?*.

### Files to create

- `src/pyleans/pyleans/transport/tcp/manager.py`

### Design

```python
# manager.py
class SiloConnectionManager:
    def __init__(
        self,
        local_silo: SiloAddress,
        cluster_id: ClusterId,
        options: TransportOptions,
        message_handler: MessageHandler,
    ) -> None:
        self._connections: dict[SiloAddress, SiloConnection] = {}
        self._pending_connects: dict[SiloAddress, asyncio.Task] = {}
        self._reconnect_tasks: dict[SiloAddress, asyncio.Task] = {}
        self._active_silos: set[SiloAddress] = set()
        self._on_established: list[ConnectionCallback] = []
        self._on_lost: list[DisconnectionCallback] = []
        self._connect_lock = asyncio.Lock()

    async def connect(self, silo: SiloAddress) -> SiloConnection:
        """Return the existing connection or establish a new one.

        Idempotent: concurrent calls for the same silo return the same connection.
        """

    async def accept_inbound(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle an incoming connection from asyncio.start_server.

        Performs the handshake, applies dedup, installs the connection.
        """

    async def disconnect(self, silo: SiloAddress) -> None:
        """Close an existing connection and stop any reconnection attempts."""

    async def update_active_silos(self, active: set[SiloAddress]) -> None:
        """Membership changed. Open connections to new peers, close connections
        to peers no longer in the active set (reconnection attempts cancelled).
        """

    def get_connection(self, silo: SiloAddress) -> SiloConnection | None: ...
    def get_connected_silos(self) -> list[SiloAddress]: ...

    async def stop(self) -> None:
        """Cancel all reconnect tasks, close every connection gracefully."""
```

### Connection establishment flow

```
connect(target):
    if target in connections and connection is healthy: return it
    if target has a pending connect: await that task
    else: start new pending connect task
      -> open_connection(host, port, ssl=options.ssl_context, timeout=connect_timeout)
      -> send_handshake(local_silo, cluster_id, role_flags)
      -> read_handshake(timeout=handshake_timeout)
      -> validate_peer_handshake(peer, expected_cluster, expected_silo=target)
      -> apply dedup rule
      -> SiloConnection.start()
      -> register, fire on_established callbacks
```

Every failure path on the outbound side removes the pending-connect task, closes the half-open socket, and leaves the caller free to retry. The reconnect task (see below) handles "silo is briefly unreachable during startup" without the caller seeing a flap.

### Dedup rule (tie-break)

Both sides may dial simultaneously. After the handshake completes, if the manager already has a connection to the same remote silo, exactly one must survive:

> **Keep the connection initiated by the silo with the lexicographically smaller `silo_id`. Close the other.**

This ordering is purely string-compared on `silo_id` (from [task-02-01](task-02-01-cluster-identity.md) `SiloAddress.silo_id`). Both sides apply the same rule and reach the same conclusion without coordination. The loser's `close()` is graceful -- no pending requests should exist yet because no frames have been sent on the duplicate.

This is subtle enough that the test suite MUST cover it explicitly: two manager instances in the same event loop, each told to connect to the other simultaneously, must end up with exactly one `SiloConnection` between them.

### Reconnection policy

On `on_connection_lost` (delivered from `SiloConnection` via callback):

```python
async def _reconnect_loop(silo: SiloAddress):
    attempt = 0
    while silo in self._active_silos:
        delay = min(base_delay * (2 ** attempt), max_delay)
        jitter = random.uniform(-delay * jitter_fraction, delay * jitter_fraction)
        await asyncio.sleep(max(0.0, delay + jitter))
        try:
            await self.connect(silo)
            return                # success, reconnect loop exits
        except (HandshakeError, TransportConnectionError) as e:
            log.warning("reconnect to %s failed (attempt %d): %s", silo, attempt, e)
            attempt += 1
```

Policy details grounded in research:

- **Exponential backoff with jitter** matches the [AWS Architecture Blog recommendation](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/) and gRPC A6. Without jitter, N silos that lost the same peer in the same instant all reconnect at exactly the same time -- synchronized thundering herd.
- **No maximum attempt count** while the peer is in active membership. The membership layer owns the "peer is dead" decision; the transport has no business giving up independently. When the membership layer removes the silo from `active_silos`, the loop exits.
- **No retry after `HandshakeError` due to cluster-id mismatch** -- that is configuration, not transient. This is surfaced with a dedicated subclass so the manager can tell the two apart.
- **Idempotent**: multiple calls to start a reconnect loop for the same silo coalesce to one.

### Callbacks

Registration is via `on_connection_established(cb)` / `on_connection_lost(cb)` on the manager. Consumers (directory cache, failure detector) register once at silo startup. Callbacks:

- Called on the event loop's scheduling path, not inside the read loop of the affected connection -- dispatch via `asyncio.create_task` so a slow callback cannot stall the manager.
- Exceptions in callbacks are logged and swallowed. One misbehaving observer MUST NOT cascade into the transport.

### Membership updates

`update_active_silos(active)` is the bridge from the failure detector to the transport:

- Peers newly in `active` but not in `connections`: launch `connect()` eagerly (don't wait for first `send_request`).
- Peers in `connections` but not in `active`: close gracefully, cancel their reconnect task.
- Peers already in both: no-op.

Eager connects mean cluster steady state has all mesh links open, so the first cross-silo grain call does not pay connection-setup latency.

### Shutdown

`stop()` cancels every reconnect task, iterates connections and awaits `close("normal")` on each with a total deadline of `options.shutdown_timeout` (default 10 s). After deadline, remaining connections are forced closed.

### Acceptance criteria

- [x] `connect(s)` returns a connection after a successful handshake
- [x] `connect(s)` called concurrently from 10 tasks results in exactly one `open_connection` call
- [x] `accept_inbound` validates handshake and registers the connection
- [x] Simultaneous dial from both sides produces exactly one live connection -- the one initiated by the smaller silo_id
- [x] `connect(s)` with mismatched cluster_id raises `HandshakeError` and is NOT retried
- [x] Connection loss triggers `on_lost` callback and schedules a reconnect
- [x] Reconnect loop stops when the silo leaves `active_silos`
- [x] Reconnect delays follow exponential backoff with jitter (test by instrumenting `asyncio.sleep`)
- [x] `update_active_silos` opens new connections and closes removed ones
- [x] `stop()` closes all connections and cancels all reconnect tasks
- [x] A callback that raises does NOT disrupt the manager
- [x] Unit tests cover: handshake failure paths (magic, version, cluster, silo), dedup race, graceful shutdown mid-reconnect, callback exception isolation

## Findings of code review

- **[resolved] Re-raise of `asyncio.CancelledError` in `_monitor_connection` was redundant under Python 3.12** (pylint `W0706`); removed. The `await conn.wait_closed()` is the only statement that can raise, and cancellation propagates naturally without the handler.
- **[resolved] `TransportOptions` lacked `shutdown_timeout`** (named by the task spec for manager `stop()`). Added with default 10 s alongside the other connection-layer knobs from task-02-06.
- **[resolved] `asyncio.TimeoutError` aliases** replaced with builtin `TimeoutError` per ruff `UP041` (Python 3.11+ aliases `asyncio.TimeoutError = TimeoutError`, so using the stdlib name is canonical).
- **[resolved] `stop()` mixed `Task[None]` and `Task[SiloConnection]` in one loop**, tripping mypy's invariance check. Unified via `list[asyncio.Task[object]]` — the tasks are only cancelled and awaited, the return type is never inspected.
- **[noted] `SiloAddress.from_silo_id` does not handle IPv6 literals** (colon-bearing host). IPv6 is explicitly out of PoC scope; documented in the method's docstring.
- **[noted] `_install_connection` dedup uses `is_closed` as the liveness check**, not heartbeat RTT. A connection that's technically "running" but unresponsive would still win the race until the failure detector marks its peer suspect. This is by design: the manager is a pure connection-lifecycle layer; liveness decisions belong to task-02-11 (failure detector).

## Findings of security review

- **[reviewed, no change] Handshake is validated before any bytes of application traffic flow.** Inbound flow:
  `read_handshake(timeout)` → `validate_peer_handshake(cluster_id)` → write our handshake → only then call `_install_connection`.
  Outbound flow:
  `open_connection(timeout=connect_timeout)` → write our handshake → `read_handshake(timeout=handshake_timeout)` → `validate_peer_handshake(expected_silo=dialled)` → install.
  Peer is therefore never able to inject `TransportMessage` frames until it has proven protocol version, cluster id, and (on outbound) target silo id.
- **[reviewed, no change] Handshake errors are treated as unrecoverable by the reconnect loop.** A `HandshakeError` (magic / version / cluster / silo mismatch, malformed / truncated payload) aborts the reconnect loop with an ERROR log rather than spinning forever: the task spec's "cluster-id mismatch is configuration, not transient" rule is honoured uniformly for *every* handshake failure mode.
- **[reviewed, no change] Inbound handshake timeout + bounded payload limits** (task-02-05 `MAX_HANDSHAKE_STRING=256`, `handshake_timeout` default 5 s) cap the resources a hostile dialler can tie up. A silent peer cannot block an inbound slot indefinitely.
- **[reviewed, no change] Callback sandboxing.** `_safely_invoke` wraps every `on_established` / `on_lost` callback in a broad `except Exception` with `logger.exception`. A raising callback produces a single log line and never propagates to the manager's event-loop tasks. Covered by `TestCallbacks::test_callback_exception_does_not_disrupt_manager`.
- **[reviewed, no change] Dedup is deterministic and stateless.** The rule "keep the connection initiated by the smaller `silo_id`" is purely string-compared; both sides reach the same decision without coordination. No token or epoch is required, so there's no window in which a replayed inbound handshake could steal an established link -- the existing connection either wins by id (new drops) or loses by id (existing is closed gracefully, no frames have flowed on the new path yet).
- **[reviewed, no change] Reconnect backoff uses `random.uniform` (non-cryptographic).** Appropriate: jitter's purpose is thundering-herd avoidance, not security; cryptographic randomness would add cost for no benefit.
- **[reviewed, no change] Resource bounds.** The manager owns at most one `SiloConnection` per active silo, plus at most one reconnect/monitor task per silo. With `active_silos` bounded by the cluster size, the manager's footprint is O(N) silos with no unbounded growth paths.
- **[reviewed, no change] Secret-logging rule (CLAUDE.md).** No log line emits handshake payload bytes, message headers, or bodies -- only silo identities, attempt counters, and exception strings.

## Summary of implementation

### Files created

- [src/pyleans/pyleans/transport/tcp/manager.py](../../src/pyleans/pyleans/transport/tcp/manager.py) -- `SiloConnectionManager`: per-peer `SiloConnection` ownership, outbound/inbound handshake, dedup, reconnect with exponential backoff + jitter, active-silo bookkeeping, graceful shutdown. Injects `sleep` for deterministic test timing.
- [src/pyleans/test/test_transport_manager.py](../../src/pyleans/test/test_transport_manager.py) -- 16 AAA-labelled tests covering every acceptance bullet, wired entirely over `InMemoryNetwork`. Each endpoint fixture spins up a manager + simulated server that routes inbound connections to `manager.accept_inbound`.

### Files modified

- [src/pyleans/pyleans/transport/tcp/__init__.py](../../src/pyleans/pyleans/transport/tcp/__init__.py) -- re-exports `SiloConnectionManager`.
- [src/pyleans/pyleans/transport/tcp/connection.py](../../src/pyleans/pyleans/transport/tcp/connection.py) -- `SiloConnection` gains `wait_closed() -> _CloseReason`, `close_reason` property, and a `_closed_event`. The manager uses these to monitor link-state changes without polling.
- [src/pyleans/pyleans/identity.py](../../src/pyleans/pyleans/identity.py) -- `SiloAddress.from_silo_id(str)` classmethod parses the `host:port:epoch` wire form back into a `SiloAddress`; used by `accept_inbound` to derive the remote address from the peer's handshake.
- [src/pyleans/pyleans/transport/options.py](../../src/pyleans/pyleans/transport/options.py) -- new `shutdown_timeout: float = 10.0` (default from the task spec), alongside the task-02-06 connection knobs.

### Key implementation decisions

- **Dedup is tie-broken on `initiator` silo, not on which connection object arrived first.** Storing `_initiators: dict[SiloAddress, SiloAddress]` alongside `_connections` makes the tie-break deterministic regardless of event-loop scheduling order. Same rule applied on both ends yields the same surviving connection without protocol-level coordination.
- **Reconnect only fires on `"lost"` close_reason.** `_monitor_connection` awaits `conn.wait_closed()`; if the reason is `"normal"` (e.g. `manager.disconnect()` triggered it), no reconnect is scheduled. Reconnection is a response to transport failure, not to deliberate teardown.
- **`sleep` is an injected callable** (`_SleepFn`, default `asyncio.sleep`). Tests replace it with an instrumented fake that records delays, producing deterministic verification of the exponential-backoff curve (`0.01, 0.02, 0.04, 0.08, 0.08` with `max_delay=0.08`, `jitter_fraction=0`).
- **Eager connects for newly-active silos happen as fire-and-forget tasks** (`_eager_connect`) so `update_active_silos` returns promptly. A failed eager connect falls through to the reconnect loop for as long as the silo remains active.
- **Handshake failures short-circuit the reconnect loop.** `HandshakeError` -> `logger.error` + return. Only transient `TransportConnectionError` / `OSError` increment the attempt counter.
- **`_install_connection` is the single choke point for dedup.** Both the outbound connect path and the inbound accept path funnel through it, so the dedup rule is applied exactly once per potential duplicate regardless of who initiated which side.

### Deviations from the original design

- **No `shutdown_timeout` in TransportOptions originally** — the task spec referenced `options.shutdown_timeout` but the field didn't exist. Added rather than hardcoded, matching the task-spec intent.
- **Monitor-task approach** instead of direct callback wiring from `SiloConnection`. The spec's prose described "on `on_connection_lost` (delivered from `SiloConnection` via callback)"; I implemented this as a per-connection monitor coroutine that awaits `conn.wait_closed()`. Functionally identical, simpler to reason about under cancellation and dedup.

### Test coverage summary

- 16 new tests in `test_transport_manager.py`. Each is AAA-labelled with exactly one Act.
- Full suite: **622 passed** (up from 606 after task-02-06).
- `ruff check` + `ruff format --check` clean.
- `pylint src/pyleans/pyleans src/counter_app src/counter_client` rated **10.00/10**.
- `mypy` errors unchanged at 24 (all in pre-existing, unrelated files).
