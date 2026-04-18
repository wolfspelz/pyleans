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

- [ ] Two `TcpClusterTransport` instances on localhost establish a connection both directions and exchange `send_request` frames
- [ ] `send_one_way` reaches the peer handler with a `ONE_WAY` direction
- [ ] `send_ping` returns a reasonable RTT (< 1 s on localhost)
- [ ] `on_connection_established` fires exactly once per directional pair after handshake
- [ ] `on_connection_lost` fires when peer is killed
- [ ] `send_request` to `local_silo` raises `ValueError`
- [ ] `stop()` closes the accept socket and all connections; subsequent `send_request` raises `TransportClosedError`
- [ ] `TcpTransportFactory().create_cluster_transport(options)` returns a working transport
- [ ] Integration test walks the full failure-mode matrix above
- [ ] Unit tests cover lifecycle (start twice raises, stop before start is no-op, handler missing)

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
