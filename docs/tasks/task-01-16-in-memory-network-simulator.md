# Task 01-16: `InMemoryNetwork` Simulator

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-15-network-port.md](task-01-15-network-port.md)

## References
- [adr-network-port-for-testability](../adr/adr-network-port-for-testability.md)
- [adr-provider-interfaces](../adr/adr-provider-interfaces.md)
- Research notes: canonical `asyncio.StreamReader.feed_data()` pattern; `trio.testing.memory_stream_pair` as a reference design; AnyIO memory streams for API comparison

## Description

Land the in-memory `InMemoryNetwork` adapter that satisfies the testing half of [adr-network-port-for-testability](../adr/adr-network-port-for-testability.md). The port ABC and production adapter already exist from [task-01-15](task-01-15-network-port.md); this task adds the test-side implementation so that every subsequent Phase 1 task that touches networking — Silo, Counter App, Counter Client — writes its tests against the simulator from day one. The entire Phase 1 test suite will run with **zero OS-level TCP ports bound, zero file descriptors consumed, zero kernel involvement**.

The simulator is faithful enough that tests exercise every behavior pyleans depends on in production: ordered byte delivery per connection, `drain()`-based backpressure above a high-water mark, standard stdlib exception types on failure (`ConnectionRefusedError`, `ConnectionResetError`, `IncompleteReadError`), and asyncio-parity server lifecycle. Explicit failure-injection hooks let tests deterministically reproduce error paths that are flaky or impossible to trigger against real sockets (mid-write peer reset, asymmetric half-close, sudden connection refused).

### Files to create

- `src/pyleans/pyleans/net/memory_network.py` -- `InMemoryNetwork`, `InMemoryServer`, `MemoryStreamWriter`, `_MemorySocketInfo`, virtual-address bookkeeping
- `src/pyleans/test/test_memory_network.py` -- isolated unit tests for the adapter
- `src/pyleans/pyleans/net/__init__.py` -- additive re-export of `InMemoryNetwork`
- `src/pyleans/pyleans/__init__.py` -- additive re-export of `InMemoryNetwork`

### Design

#### `InMemoryNetwork` -- the registry

```python
class InMemoryNetwork(INetwork):
    """In-process network simulator. One instance per test.

    Servers register themselves under (host, port). open_connection looks
    up the server and wires a pair of memory-backed StreamReader/Writer
    to the server's handler. No OS sockets are ever created.
    """

    def __init__(self) -> None:
        self._servers: dict[tuple[str, int], InMemoryServer] = {}
        self._next_virtual_port = 40000  # per-instance, out of real ephemeral range

    async def start_server(
        self, client_connected_cb, host, port, *, ssl=None,
    ) -> NetworkServer:
        if ssl is not None:
            logger.debug("InMemoryNetwork: TLS not simulated")
        if port == 0:
            port = self._allocate_virtual_port()
        key = (host, port)
        if key in self._servers:
            raise OSError(errno.EADDRINUSE, f"address in use: {host}:{port}")
        server = InMemoryServer(key, client_connected_cb, owner=self)
        self._servers[key] = server
        return server

    async def open_connection(self, host, port, *, ssl=None):
        if ssl is not None:
            logger.debug("InMemoryNetwork: TLS not simulated")
        server = self._servers.get((host, port))
        if server is None or server.is_closed():
            raise ConnectionRefusedError(
                f"in-memory: no server at {host}:{port}"
            )
        client_reader, server_writer = _make_stream_pair(
            local=("127.0.0.1", self._allocate_virtual_port()),
            remote=(host, port),
        )
        server_reader, client_writer = _make_stream_pair(
            local=(host, port),
            remote=client_writer.get_extra_info("sockname"),
        )
        server.handle_incoming(server_reader, server_writer)
        return client_reader, client_writer
```

The virtual-port counter is **per-instance**, not module-global, so parallel pytest-xdist workers each get their own counter without collisions. Starting at `40000` keeps virtual ports out of the real ephemeral range (32768+ on Linux) in log output, so `localhost:40001` in a log line is visibly synthetic.

#### `InMemoryServer` -- asyncio-parity lifecycle

```python
class InMemoryServer(NetworkServer):
    def __init__(self, address, handler, owner): ...

    def close(self) -> None:
        # Stops accepting new connections. Does NOT cancel in-flight handler
        # tasks -- matches asyncio.Server semantics so shutdown-race bugs
        # the abstraction is meant to expose are not masked.
        self._closed = True
        self._owner._unregister(self._address)

    async def wait_closed(self) -> None:
        await asyncio.gather(*self._handler_tasks, return_exceptions=True)

    @property
    def sockets(self) -> list[_MemorySocketInfo]:
        return [_MemorySocketInfo(self._address)]

    def handle_incoming(self, reader, writer) -> None:
        if self._closed:
            writer.close()
            return
        task = asyncio.create_task(self._handler(reader, writer))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)
```

`_MemorySocketInfo.getsockname()` returns the virtual `(host, port)` tuple so a consumer reading `server.sockets[0].getsockname()[1]` to discover its assigned port (e.g. the `GatewayListener.port` property in [task-01-17](task-01-17-silo.md)) keeps working identically whether the adapter is `AsyncioNetwork` or `InMemoryNetwork`.

#### `MemoryStreamWriter` -- real backpressure, real failure modes

The write half is a minimal `StreamWriter`-compatible object backed by a bounded buffer. Real backpressure, not a no-op:

```python
class MemoryStreamWriter:
    def __init__(
        self,
        peer_reader: asyncio.StreamReader,
        *,
        sockname: tuple[str, int],
        peername: tuple[str, int],
        high_water: int = 64 * 1024,
        low_water: int = 16 * 1024,
    ) -> None: ...

    def write(self, data: bytes) -> None:
        if self._closing or self._reset_pending:
            raise ConnectionResetError("peer reset")
        self._buffered += len(data)
        self._peer_reader.feed_data(data)
        if self._buffered >= self._high_water:
            self._drain_waiter = asyncio.get_event_loop().create_future()

    async def drain(self) -> None:
        if self._reset_pending:
            raise ConnectionResetError("peer reset")
        if self._drain_waiter is not None:
            await self._drain_waiter

    def close(self) -> None:
        if self._closing: return
        self._closing = True
        self._peer_reader.feed_eof()

    async def wait_closed(self) -> None: ...

    def is_closing(self) -> bool: return self._closing

    def get_extra_info(self, name: str, default=None):
        return {"peername": self._peername, "sockname": self._sockname}.get(name, default)

    # --- failure injection (test-only hooks) ---

    def simulate_reset(self) -> None:
        """Next write / drain raises ConnectionResetError."""
        self._reset_pending = True

    def simulate_peer_close(self) -> None:
        """Peer reader sees IncompleteReadError on next readexactly."""
        self._peer_reader.feed_eof()
```

**`drain()` is not a no-op** — a no-op would silently hide flow-control bugs the simulator is specifically meant to catch. The high/low-water-mark pattern mirrors `asyncio.StreamWriter.drain()` semantics so Phase 2's backpressure-dependent code (bounded pending-request semaphore + write buffer HWM in [task-02-06](task-02-06-silo-connection.md) and [task-02-07](task-02-07-silo-connection-manager.md)) can be exercised with real yielding behavior under the simulator.

#### Failure-mode parity matrix

All exceptions are **real stdlib types**, not custom subclasses, so consumers' existing `except OSError` / `except ConnectionError` / `except asyncio.IncompleteReadError` clauses work identically against both adapters:

| Production failure                         | Simulator hook                                   | Exception raised                                        |
|--------------------------------------------|--------------------------------------------------|---------------------------------------------------------|
| Peer process killed                        | `writer.simulate_reset()`                        | `ConnectionResetError` (OSError subclass) on next `write`/`drain` |
| Peer called `close()` mid-stream           | `writer.simulate_peer_close()`                   | `asyncio.IncompleteReadError` on next `readexactly`; `read()` returns `b""` |
| Server never bound                         | `open_connection` against unregistered address   | `ConnectionRefusedError` (OSError subclass)             |
| Two servers on same address                | `start_server` against registered address        | `OSError(errno=EADDRINUSE)`                             |

#### What the simulator deliberately does NOT cover

Per [adr-network-port-for-testability](../adr/adr-network-port-for-testability.md):

- Packet-level chaos (latency, loss, reorder) — simulate at the application layer above the port when a specific test needs it.
- TLS. `ssl` parameter accepted for API parity, logged at DEBUG, otherwise ignored. Attempting to simulate TLS in-memory would be snake-oil.
- Multi–event-loop / multi-process scenarios.
- Performance characterisation.

### Canonical test-fixture pattern

Downstream tasks' tests construct both the server component (e.g. Silo) and the client component (e.g. ClusterClient) with **the same `InMemoryNetwork` instance** so they discover each other via the shared registry. The conventional pattern, used from [task-01-17](task-01-17-silo.md) onward:

```python
# src/pyleans/test/conftest.py
import pytest
from pyleans.net import InMemoryNetwork

@pytest.fixture
def network() -> InMemoryNetwork:
    return InMemoryNetwork()
```

```python
async def test_round_trip(network: InMemoryNetwork) -> None:
    silo = Silo(..., network=network, gateway_port=0)
    await silo.start_background()
    try:
        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()
        ...
    finally:
        await client.close()
        await silo.stop()
```

One network instance per test → isolated registries, no cross-test leakage.

### Regression guard

A small pytest plugin hook (or a ruff rule) that scans each collected test module's source for direct imports of `asyncio.start_server` or `asyncio.open_connection`, and fails collection with a clear message pointing at `INetwork`. Prevents future contributors from accidentally bypassing the port and reintroducing real-socket tests.

### Acceptance criteria

**Core simulator behavior (`test_memory_network.py`):**

- [ ] `start_server` + `open_connection` round-trip: bytes written to one side arrive at the other, in order
- [ ] `open_connection` against unbound address raises `ConnectionRefusedError`
- [ ] `start_server` twice on the same address raises `OSError` with `errno.EADDRINUSE`
- [ ] `port=0` assigns a per-instance monotonic virtual port ≥40000
- [ ] `writer.simulate_reset()` causes next `write` or `drain` to raise `ConnectionResetError`
- [ ] `writer.simulate_peer_close()` causes peer `readexactly` to raise `IncompleteReadError`
- [ ] `drain()` actually blocks above the high-water mark and releases below the low-water mark (test with explicit reads on the peer reader)
- [ ] `server.close()` stops accepting; subsequent `open_connection` fails
- [ ] `server.wait_closed()` awaits in-flight handler tasks to completion; does NOT cancel them
- [ ] `writer.get_extra_info("peername")` / `("sockname")` return the virtual addresses
- [ ] `ssl` parameter is accepted and logged at DEBUG, not raised

**Integration with the port:**

- [ ] `InMemoryNetwork` is a full `INetwork` implementation (no unimplemented methods)
- [ ] Shared instance between two consumers in the same test routes traffic correctly

**Regression guard:**

- [ ] Canary test: intentionally importing `asyncio.start_server` in a test module triggers a collection failure with a message pointing to `INetwork`

**Quality gates:**

- [ ] `ruff`, `pylint` (10.00/10), `mypy` all clean

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
