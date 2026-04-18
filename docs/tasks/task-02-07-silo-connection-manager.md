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

- [ ] `connect(s)` returns a connection after a successful handshake
- [ ] `connect(s)` called concurrently from 10 tasks results in exactly one `open_connection` call
- [ ] `accept_inbound` validates handshake and registers the connection
- [ ] Simultaneous dial from both sides produces exactly one live connection -- the one initiated by the smaller silo_id
- [ ] `connect(s)` with mismatched cluster_id raises `HandshakeError` and is NOT retried
- [ ] Connection loss triggers `on_lost` callback and schedules a reconnect
- [ ] Reconnect loop stops when the silo leaves `active_silos`
- [ ] Reconnect delays follow exponential backoff with jitter (test by instrumenting `asyncio.sleep`)
- [ ] `update_active_silos` opens new connections and closes removed ones
- [ ] `stop()` closes all connections and cancels all reconnect tasks
- [ ] A callback that raises does NOT disrupt the manager
- [ ] Unit tests cover: handshake failure paths (magic, version, cluster, silo), dedup race, graceful shutdown mid-reconnect, callback exception isolation

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
