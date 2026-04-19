# Task 02-04: Transport Ports -- `IClusterTransport` ABC, Message Types, Error Hierarchy

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-01-cluster-identity.md](task-02-01-cluster-identity.md)
- [task-01-15-network-port.md](task-01-15-network-port.md) -- `TransportOptions` carries an `INetwork` so the TCP transport and its unit tests share the same port

## References
- [adr-cluster-transport](../adr/adr-cluster-transport.md)
- [architecture/pyleans-transport.md](../architecture/pyleans-transport.md) -- §6 abstract transport interface
- [orleans-networking.md](../orleans-architecture/orleans-networking.md) -- §5 message format, §9 correlation, §10 one-way

## Description

Define the **port** that the grain runtime, failure detector, and distributed directory all depend on for silo-to-silo communication. Everything above this port must not know that the default adapter is TCP -- swapping TCP for MQTT, Zenoh, or Unix sockets later must be an additive change.

Concretely, this task ships **only ABCs, value types, and the error hierarchy**. The concrete TCP implementation is wired across [task-02-05](task-02-05-wire-protocol.md), [task-02-06](task-02-06-silo-connection.md), [task-02-07](task-02-07-silo-connection-manager.md), and [task-02-08](task-02-08-tcp-cluster-transport.md). Landing the port first freezes the contract the downstream work targets.

The design mirrors [architecture/pyleans-transport.md §6](../architecture/pyleans-transport.md) but lives in code with docstrings that survive independent of the architecture doc.

### Files to create

- `src/pyleans/pyleans/transport/__init__.py` -- re-exports
- `src/pyleans/pyleans/transport/messages.py` -- `TransportMessage`, `MessageDirection`, `MessageType`
- `src/pyleans/pyleans/transport/options.py` -- `TransportOptions`
- `src/pyleans/pyleans/transport/errors.py` -- `TransportError` hierarchy
- `src/pyleans/pyleans/transport/cluster.py` -- `IClusterTransport` ABC

### Design

```python
# messages.py
from dataclasses import dataclass
from enum import IntEnum

class MessageType(IntEnum):
    REQUEST  = 0x01
    RESPONSE = 0x02
    ONE_WAY  = 0x03
    PING     = 0x04
    PONG     = 0x05
    ERROR    = 0x06


@dataclass(frozen=True)
class TransportMessage:
    """Opaque message exchanged across the transport.

    `header` and `body` are bytes from the layer above -- the transport
    never inspects their contents. This keeps the transport agnostic of
    message schema evolution.
    """
    message_type: MessageType
    correlation_id: int       # uint64; 0 for ONE_WAY
    header: bytes
    body: bytes
```

```python
# options.py
import ssl
from dataclasses import dataclass

@dataclass
class TransportOptions:
    max_message_size: int = 16 * 1024 * 1024       # 16 MB -- reject larger, see task-02-05
    default_request_timeout: float = 30.0          # matches Orleans default
    max_in_flight_requests: int = 1000             # per-connection backpressure
    keepalive_interval: float = 30.0               # idle PING cadence
    keepalive_timeout: float = 10.0                # PONG deadline
    reconnect_base_delay: float = 0.1              # exponential backoff base
    reconnect_max_delay: float = 30.0
    reconnect_jitter_fraction: float = 0.3         # ±30% jitter on computed delay
    connect_timeout: float = 5.0
    handshake_timeout: float = 5.0
    ssl_context: ssl.SSLContext | None = None
    network: INetwork = field(default_factory=AsyncioNetwork)   # see task-01-15
```

The `network` field (from [task-01-15](task-01-15-network-port.md)) is how the TCP transport stays testable without real sockets: production uses the default `AsyncioNetwork`; tests construct `TransportOptions(network=InMemoryNetwork())` to run the entire mesh in-process. See [adr-network-port-for-testability](../adr/adr-network-port-for-testability.md).

```python
# errors.py
class TransportError(Exception):
    """Base class for every error originating in the transport layer."""

class HandshakeError(TransportError):
    """Peer rejected the handshake or sent an invalid handshake."""

class MessageTooLargeError(TransportError):
    """Frame exceeds max_message_size; connection is terminated."""

class BackpressureError(TransportError):
    """Would exceed max_in_flight_requests while caller requested non-blocking."""

class TransportTimeoutError(TransportError, TimeoutError):
    """Request timed out waiting for a response."""

class TransportConnectionError(TransportError, ConnectionError):
    """Connection lost, refused, or unreachable."""

class TransportClosedError(TransportError):
    """Transport has been stopped; no further sends accepted."""
```

Deriving `TransportTimeoutError` from the stdlib `TimeoutError` and `TransportConnectionError` from `ConnectionError` means the grain runtime can write normal `except TimeoutError:` / `except ConnectionError:` handlers without importing transport-specific types -- important for keeping the runtime decoupled from the transport adapter.

```python
# cluster.py
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pyleans.cluster import ClusterId
from pyleans.identity import SiloAddress
from pyleans.transport.messages import TransportMessage


MessageHandler = Callable[
    [SiloAddress, TransportMessage],
    Awaitable[TransportMessage | None],
]
ConnectionCallback = Callable[[SiloAddress], Awaitable[None]]
DisconnectionCallback = Callable[[SiloAddress, Exception | None], Awaitable[None]]


class IClusterTransport(ABC):
    """Port for silo-to-silo communication.

    Contract guarantees the transport adapter MUST honor:
      - send_request/send_one_way/send_ping never return before the message
        has been accepted by the adapter (but see send_request below).
      - In-order per-peer delivery: messages sent to the same silo on the
        same logical send pipeline arrive in send order.
      - Fail-fast on disconnect: all outstanding send_request futures fault
        with TransportConnectionError the moment the underlying connection
        is classified as lost. The adapter MUST NOT silently retry.
      - Reconnection is the adapter's responsibility while the peer remains
        in the active silo set provided by the membership layer.

    Lifecycle:
        tx = factory.create_cluster_transport(options)
        await tx.start(local_silo, cluster_id, handler)
        # ... use send_request / send_one_way ...
        await tx.stop()
    """

    @abstractmethod
    async def start(
        self,
        local_silo: SiloAddress,
        cluster_id: ClusterId,
        message_handler: MessageHandler,
    ) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def connect_to_silo(self, silo: SiloAddress) -> None: ...

    @abstractmethod
    async def disconnect_from_silo(self, silo: SiloAddress) -> None: ...

    @abstractmethod
    async def send_request(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]: ...

    @abstractmethod
    async def send_one_way(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
    ) -> None: ...

    @abstractmethod
    async def send_ping(self, target: SiloAddress, timeout: float = 10.0) -> float:
        """Return measured round-trip time in seconds."""

    @abstractmethod
    def is_connected_to(self, silo: SiloAddress) -> bool: ...

    @abstractmethod
    def get_connected_silos(self) -> list[SiloAddress]: ...

    @property
    @abstractmethod
    def local_silo(self) -> SiloAddress: ...

    @abstractmethod
    def on_connection_established(self, cb: ConnectionCallback) -> None: ...

    @abstractmethod
    def on_connection_lost(self, cb: DisconnectionCallback) -> None: ...


class ITransportFactory(ABC):
    """Creates transport instances. Consumed by the silo builder."""

    @abstractmethod
    def create_cluster_transport(self, options: TransportOptions) -> IClusterTransport: ...
```

### Why these callback events are first-class

`on_connection_established` / `on_connection_lost` are the integration point for the failure detector ([task-02-11](task-02-11-failure-detector.md)) and the directory ([task-02-14 cache invalidation](task-02-14-directory-cache.md)). Making them part of the port contract -- rather than letting each consumer poll `get_connected_silos()` -- keeps those consumers event-driven and lets us test membership-driven behaviors in isolation by driving callbacks directly.

### Handler return contract

`MessageHandler` returns `TransportMessage | None`:
- For `MessageType.REQUEST` input, the handler **must** return a `TransportMessage` with `message_type=RESPONSE` and the matching `correlation_id`. If the application raises, the adapter wraps it into a `MessageType.ERROR` response (error taxonomy in task-02-05).
- For `MessageType.ONE_WAY` input, the handler returns `None`.
- The handler never sees `PING` / `PONG` / `ERROR` -- those are handled inside the adapter.

This invariant is enforced by the adapter layer so the runtime never has to write defensive checks around the handler contract.

### Acceptance criteria

- [x] All ABCs non-instantiable; abstract methods listed above
- [x] `TransportMessage` is frozen, hashable, round-trips field equality
- [x] `TransportOptions` defaults match the architecture doc §6.1
- [x] `TransportTimeoutError` is-a `TimeoutError`, `TransportConnectionError` is-a `ConnectionError`
- [x] `MessageType` values match the wire codepoints in [architecture/pyleans-transport.md §4.3](../architecture/pyleans-transport.md) so task-02-05 can reuse the enum directly
- [x] Unit tests: instantiation failures, callback registration signature checks, dataclass frozen-ness, error hierarchy isinstance checks

## Findings of code review

- **Clean code / SOLID**: each file has exactly one responsibility — messages, errors, options, ABCs. ABCs are thin (all methods abstract); no default implementations leak into the port.
- **Type hints**: fully typed; callback aliases (`MessageHandler`, `ConnectionCallback`, `DisconnectionCallback`) carry `Awaitable` return types so any non-coroutine implementation fails mypy at the consumer.
- **Hexagonal architecture**: the package ships *only* ports. The concrete TCP adapter (02-06..02-08) imports this; the port does not depend on any adapter. `TransportOptions` takes an `INetwork` — the parallel port from Phase 1 — so the whole mesh stays testable without OS sockets.
- **Naming**: `IClusterTransport`, `ITransportFactory` use the Phase 2 port-naming convention; `MessageType`, `MessageDirection` are PascalCase IntEnums; constants (`DEFAULT_*`) in UPPER_SNAKE.
- **Tests**: AAA labels; exactly one Act per test; covers frozen-ness, equality, hashability, int enum wire values, each error hierarchy relation, `TimeoutError` / `ConnectionError` interoperability via `pytest.raises`, ABC instantiation refusal, abstract method set, factory abstract method, callback parameter signatures.
- **No dead code, no unused imports.** No logging is emitted from the port itself — adapters log at their own boundaries.

No issues raised.

## Findings of security review

- **Input validation at the boundary**: the port does not accept untrusted input; adapters validate framing (task-02-05) and handshake (task-02-08). `TransportOptions` fields are plain numeric / `ssl.SSLContext` values — adapters enforce ranges where required.
- **Unbounded resource consumption**: `max_message_size` (16 MB) and `max_in_flight_requests` (1000) cap memory per connection; the default is an explicit cap, not a silently unlimited value.
- **TLS hook**: `ssl_context` is an optional `ssl.SSLContext` — adapters that support TLS must consume it. Port definition alone does not weaken security posture.
- **No file I/O, no subprocess, no network**; the port is a pure contract module.

No vulnerabilities found.

## Summary of implementation

### Files created / modified

- [src/pyleans/pyleans/transport/__init__.py](../../src/pyleans/pyleans/transport/__init__.py) — re-exports the package surface.
- [src/pyleans/pyleans/transport/messages.py](../../src/pyleans/pyleans/transport/messages.py) — `MessageType`, `MessageDirection`, frozen `TransportMessage`.
- [src/pyleans/pyleans/transport/errors.py](../../src/pyleans/pyleans/transport/errors.py) — `TransportError` and six subclasses with stdlib-base mixins for `TimeoutError` / `ConnectionError`.
- [src/pyleans/pyleans/transport/options.py](../../src/pyleans/pyleans/transport/options.py) — `TransportOptions` dataclass defaulting to `AsyncioNetwork`.
- [src/pyleans/pyleans/transport/cluster.py](../../src/pyleans/pyleans/transport/cluster.py) — `IClusterTransport`, `ITransportFactory`, callback aliases with contract docs.
- [src/pyleans/test/test_transport_ports.py](../../src/pyleans/test/test_transport_ports.py) — 20 tests covering the full port surface.

### Key decisions

- **One file per concern.** The spec listed five files; I kept that split. It makes the port easy to read and gives each concern a clean import path (`pyleans.transport.errors` vs `pyleans.transport.cluster` vs …).
- **`MessageType` IntEnum values chosen to match the wire codec.** The 02-05 wire-protocol task will read `message_type: int` directly off the wire and cast to `MessageType`, so the enum value is the source of truth. No separate codec mapping table.
- **Dual-parent error classes (`TransportTimeoutError(TransportError, TimeoutError)`).** The grain runtime already writes `except TimeoutError:` / `except ConnectionError:` handlers; importing `TransportError` in the runtime would couple it to the transport adapter. The dual-parent approach lets callers stay on stdlib types while keeping adapter-specific context available.
- **`TransportOptions` is mutable (`@dataclass`, not frozen).** The silo builder may want to adjust options after construction (e.g. in integration tests) without reconstructing the whole object. Frozen buys nothing here; the adapter copies every field it needs at `start()` time.
- **`default_factory=AsyncioNetwork`** for the `network` field means production code gets real sockets with no extra wiring, while tests opt in to `InMemoryNetwork` by naming the argument.

### Deviations

None. The port surface matches the task spec exactly.

### Test coverage summary

20 tests covering: `MessageType` wire-codepoint values; `MessageDirection` enum identities; `TransportMessage` frozen semantics, equality, hashability; `TransportOptions` defaults, default-factory network type, in-memory-network override; `TransportError` hierarchy subclass relationships; stdlib `TimeoutError` / `ConnectionError` interoperability via `pytest.raises`; `IClusterTransport` ABC refusal to instantiate and the complete set of abstract methods; `ITransportFactory` ABC refusal and abstract `create_cluster_transport`; callback-parameter signature checks on both callback registration methods.

Full suite: **515 tests** pass (Phase 1 + 02-01..02-04); `ruff check`, `ruff format --check`, `pylint` 10.00/10, `mypy` strict all clean.
