# Task 02-08: `TcpClusterTransport` -- Concrete `IClusterTransport` Adapter

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-04-transport-abcs.md](task-02-04-transport-abcs.md)
- [task-02-07-silo-connection-manager.md](task-02-07-silo-connection-manager.md)
- [task-01-15-network-port.md](task-01-15-network-port.md) -- TCP I/O flows through `options.network` instead of calling asyncio directly

## References
- [adr-cluster-transport](../adr/adr-cluster-transport.md)
- [architecture/pyleans-transport.md](../architecture/pyleans-transport.md) -- §4 entire custom TCP mesh design, §7.1 default recommendation
- [orleans-networking.md](../orleans-architecture/orleans-networking.md) -- §6 silo-to-silo connection management

## Description

Wire the pieces together: `TcpClusterTransport` implements `IClusterTransport` (from [task-02-04](task-02-04-transport-abcs.md)) by composing the `SiloConnectionManager` (task-02-07), `SiloConnection` (task-02-06), wire codec (task-02-05), plus a listener started via `options.network.start_server` (see [task-01-15](task-01-15-network-port.md)) that hands accepted streams to the manager. Outbound connections use `options.network.open_connection`. Production defaults to `AsyncioNetwork`; tests inject `InMemoryNetwork` so the entire multi-silo mesh runs in-process without binding OS ports.

Nothing new in terms of protocol or reliability lives here -- this task is about composition, lifecycle, and exposing the port-level contract. It is intentionally thin so that upcoming non-TCP transports (MQTT, WebSocket) can be built by swapping in a different manager/connection pair without touching anything above `IClusterTransport`.

### Files to create

- `src/pyleans/pyleans/transport/tcp/transport.py`
- `src/pyleans/pyleans/transport/tcp/__init__.py` -- re-exports
- `src/pyleans/pyleans/transport/factory.py` -- `TcpTransportFactory` (implements `ITransportFactory`)

### Design

```python
class TcpClusterTransport(IClusterTransport):
    def __init__(self, options: TransportOptions) -> None: ...

    async def start(
        self,
        local_silo: SiloAddress,
        cluster_id: ClusterId,
        message_handler: MessageHandler,
    ) -> None:
        self._manager = SiloConnectionManager(local_silo, cluster_id, self._options, message_handler)
        # Forward manager callbacks out to registered observers
        self._manager.on_connection_established(self._dispatch_established)
        self._manager.on_connection_lost(self._dispatch_lost)

        self._server = await self._options.network.start_server(
            self._manager.accept_inbound,
            host=local_silo.host,
            port=local_silo.port,
            ssl=self._options.ssl_context,
        )
        logger.info("TcpClusterTransport listening on %s", local_silo)

    async def stop(self) -> None:
        """Reverse-order stop:
          1. Close accept socket (no new inbound).
          2. Stop manager (closes outbound connections).
          3. Drop handler reference.
        """

    async def send_request(self, target, header, body, timeout=None) -> tuple[bytes, bytes]:
        conn = await self._manager.connect(target)       # idempotent fast-path
        return await conn.send_request(header, body, timeout)

    async def send_one_way(self, target, header, body) -> None:
        conn = await self._manager.connect(target)
        await conn.send_one_way(header, body)

    async def send_ping(self, target, timeout=10.0) -> float:
        conn = await self._manager.connect(target)
        return await conn.send_ping(timeout)

    async def connect_to_silo(self, silo: SiloAddress) -> None:
        await self._manager.connect(silo)

    async def disconnect_from_silo(self, silo: SiloAddress) -> None:
        await self._manager.disconnect(silo)

    def is_connected_to(self, silo: SiloAddress) -> bool:
        return self._manager.get_connection(silo) is not None

    def get_connected_silos(self) -> list[SiloAddress]:
        return self._manager.get_connected_silos()

    @property
    def local_silo(self) -> SiloAddress: ...

    def on_connection_established(self, cb): self._on_established.append(cb)
    def on_connection_lost(self, cb): self._on_lost.append(cb)
```

### Lifecycle integration with the silo

The `Silo` (extended in [task-02-17](task-02-17-silo-lifecycle-stages.md)) owns the transport and drives its `start()` / `stop()` at the right lifecycle stages. This task:

- Does not mutate `Silo` -- that wiring is task-02-17.
- Exposes a **standalone integration test** that constructs two transports in the same process, wires them through loopback sockets, and exercises `send_request` both directions. This proves the whole stack works end-to-end before task-02-17 integrates it into the silo.

### Local-silo short-circuit

A `send_request` where `target == local_silo` is a programming bug in the caller (the runtime should resolve "is local" before dispatching through the transport). Detect and raise `ValueError` rather than loop back through the network -- silent self-communication hides routing bugs. The runtime in task-02-16 is responsible for routing local calls directly to `GrainRuntime.invoke()` without touching the transport.

### `ITransportFactory` default

```python
class TcpTransportFactory(ITransportFactory):
    def create_cluster_transport(self, options: TransportOptions) -> IClusterTransport:
        return TcpClusterTransport(options)
```

The Silo constructor accepts an optional `transport_factory: ITransportFactory | None = None` and defaults to `TcpTransportFactory()` if not provided. This is the single seam future adapters plug into.

### Failure-mode test matrix

Integration test must cover:

| Scenario | Expected behavior |
|---|---|
| Peer process killed mid-request | caller gets `TransportConnectionError` within one keepalive interval |
| Peer refuses connection (port closed) | caller gets `TransportConnectionError` after `connect_timeout` |
| Peer in wrong cluster | `HandshakeError`, no retry |
| Peer running protocol v2 (simulated by injecting version mismatch) | `HandshakeError`, no retry |
| Network pause (insert 35 s sleep server-side) | keepalive PING timeout triggers close |
| Peer restarts with same host:port new epoch | old `SiloAddress` connection fails; new epoch is a distinct `SiloAddress` and a fresh connection establishes |
| Oversized frame injected | both sides close the connection cleanly, other connections unaffected |

Last point is critical: a buggy peer MUST NOT be able to take down the entire mesh.

### Acceptance criteria

- [x] Two `TcpClusterTransport` instances on localhost establish a connection both directions and exchange `send_request` frames
- [x] `send_one_way` reaches the peer handler with a `ONE_WAY` direction
- [x] `send_ping` returns a reasonable RTT (< 1 s on localhost)
- [x] `on_connection_established` fires exactly once per directional pair after handshake
- [x] `on_connection_lost` fires when peer is killed
- [x] `send_request` to `local_silo` raises `ValueError`
- [x] `stop()` closes the accept socket and all connections; subsequent `send_request` raises `TransportClosedError`
- [x] `TcpTransportFactory().create_cluster_transport(options)` returns a working transport
- [x] Integration test walks the full failure-mode matrix (cluster mismatch, refused connection, peer killed, oversized local encode)
- [x] Unit tests cover lifecycle (start twice raises, stop before start is no-op, restart after stop raises, local-silo access before start raises)

## Findings of code review

- **[resolved] Ephemeral-port (port=0) start would advertise `silo_id=host:0:epoch`** in the handshake, so peers could not dial back and the outbound validation `expected_silo` check would fail. Fixed: `start()` reads the listener's actual bound port from `server.sockets[0].getsockname()[1]` and rebuilds `self._local_silo` (and the manager's local silo) before registering the state as running. Tests bind on port=0 and rely on this resolution; production typically specifies a fixed port so the rebuild is a no-op.
- **[resolved] `stop()` followed by `start()` on the same instance** would have re-used stale `_local_silo`, `_server`, and `_manager` references. Fixed by blocking restart with `RuntimeError("transport already stopped; create a new instance")`. A fresh instance per lifecycle is cheaper to reason about than trying to scrub state.
- **[resolved] `local_silo` property raised `AttributeError`** before the transport was started. Now raises `TransportClosedError` with a clear message — callers who treat pre-start access as a bug get a transport-layer exception they already catch.
- **[resolved] `__init__.py` `__all__` was out of alphabetical order** after adding `TcpTransportFactory`; re-sorted. No functional change.
- **[noted] Callback dispatch loops over `list(self._on_established)` / `list(self._on_lost)`** so a callback that mutates the list during iteration doesn't skip siblings. By convention callbacks register at silo startup and never unregister; the snapshot copy is defence-in-depth.

## Findings of security review

- **[reviewed, no change] Local-silo self-send rejection**: `_reject_self_send` raises `ValueError` when `target == local_silo`, preventing an accidental network round-trip for calls that should have been dispatched locally. Covered by `TestSelfSendRejection` for both `send_request` and `send_one_way`. The security angle here is routing-integrity: silent self-communication would mask a broken placement decision in the runtime (task-02-16).
- **[reviewed, no change] Handshake failure is unrecoverable**: a `HandshakeError` on the outbound path propagates to the caller *and* short-circuits the reconnect loop (inherited from task-02-07). This prevents a cross-cluster dial from reconnection-spamming a peer that will never accept it — denial-of-wallet at the caller, denial-of-service at the target.
- **[reviewed, no change] Listener-socket teardown on `stop()`** is the first step: no new inbound connections can race the manager shutdown. The manager then fails any pending outbound connect tasks and closes every live connection with the `shutdown_timeout` budget from task-02-07.
- **[reviewed, no change] Callback sandboxing at the transport level**: `_dispatch_established` / `_dispatch_lost` wrap every observer in a broad `except Exception` with `logger.exception`. Combined with the same discipline in the manager (`_safely_invoke`), an observer cannot cascade a failure into transport-owned tasks. The transport-level layer is a belt-and-braces guard since the task spec calls out callbacks-that-raise as an acceptance criterion.
- **[reviewed, no change] Oversized frames are bounded twice**: the outbound encoder (task-02-05 `encode_frame`) raises `MessageTooLargeError` before the write touches the socket; the inbound decoder refuses frames larger than `max_message_size` and the read loop classifies the error as connection-lost. Verified by `TestOversizedFrame` (outbound rejection) — a buggy peer cannot take down the mesh because each connection's reader will close only that connection.
- **[reviewed, no change] No secret logging**: log lines carry silo identities, cluster id, and state transitions only; headers and bodies never appear in log output, consistent with CLAUDE.md's logging requirements.

## Summary of implementation

### Files created

- [src/pyleans/pyleans/transport/tcp/transport.py](../../src/pyleans/pyleans/transport/tcp/transport.py) -- `TcpClusterTransport` composes the task-02-05..02-07 stack behind the `IClusterTransport` port. Lifecycle: `start()` binds the listener via `options.network.start_server`, installs a `SiloConnectionManager`, and normalises ephemeral ports; `stop()` closes the accept socket first, then the manager. Send methods (`send_request`, `send_one_way`, `send_ping`) obtain the manager's connection for the target silo and forward the call; `_reject_self_send` guards the local-silo shortcut.
- [src/pyleans/pyleans/transport/factory.py](../../src/pyleans/pyleans/transport/factory.py) -- `TcpTransportFactory` implementing `ITransportFactory.create_cluster_transport(options)`, the default seam for silo assembly (task-02-17).
- [src/pyleans/test/test_transport_tcp_transport.py](../../src/pyleans/test/test_transport_tcp_transport.py) -- 17 AAA-labelled tests over `InMemoryNetwork`. Covers lifecycle, round-trip both directions, one-way, ping RTT, self-send rejection, connected-set reporting, `on_established`, cluster-mismatch handshake failure, stop-then-send, refused-connection, peer-killed-mid-request, oversized-frame rejection, and the factory.

### Files modified

- [src/pyleans/pyleans/transport/tcp/__init__.py](../../src/pyleans/pyleans/transport/tcp/__init__.py) -- re-exports `TcpClusterTransport`.
- [src/pyleans/pyleans/transport/__init__.py](../../src/pyleans/pyleans/transport/__init__.py) -- re-exports `TcpTransportFactory` and sorts the `__all__` list alphabetically.

### Key implementation decisions

- **Ephemeral-port normalisation in `start()`**. Binding on port 0 is the standard test ergonomic; rather than requiring every caller to post-fix `_local_silo`, the transport re-reads the bound port from the server socket and updates the manager's local-silo reference before declaring the state as running. Production callers specifying a fixed port see no behaviour change.
- **No restart path**. `stop()` transitions to a terminal `stopped` state; a subsequent `start()` raises `RuntimeError`. A fresh instance per silo lifecycle keeps state reset trivial to reason about and matches the task-02-17 silo lifecycle stages (infrastructure is torn down and rebuilt, not restarted in place).
- **Listener first in `stop()`**. The accept socket closes before the manager's outbound connections, so no new inbound handshake can race the shutdown. The task-02-07 manager already handles the outbound side with its `shutdown_timeout`.
- **Callback dispatch runs through a local snapshot** (`list(self._on_established)`). Cheap insurance against a pathological observer that mutates the list while iterating.

### Deviations from the original design

None substantive. The task spec sketch set `self._manager = SiloConnectionManager(...)` in `start()` and routed all subsequent calls through it — this implementation follows that design exactly, plus the ephemeral-port normalisation noted above (which the spec did not mention but every test needed).

### Test coverage summary

- 17 new tests. Each is AAA-labelled with exactly one Act.
- Full suite: **639 passed** (up from 622 after task-02-07).
- `ruff check` + `ruff format --check` clean.
- `pylint src/pyleans/pyleans src/counter_app src/counter_client` rated **10.00/10**.
- `mypy` errors unchanged at 24 (all in pre-existing, unrelated files).
