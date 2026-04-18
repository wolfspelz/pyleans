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

- [x] `start_server` + `open_connection` round-trip: bytes written to one side arrive at the other, in order
- [x] `open_connection` against unbound address raises `ConnectionRefusedError`
- [x] `start_server` twice on the same address raises `OSError` with `errno.EADDRINUSE`
- [x] `port=0` assigns a per-instance monotonic virtual port ≥40000
- [x] `writer.simulate_reset()` causes next `write` or `drain` to raise `ConnectionResetError`
- [x] `writer.simulate_peer_close()` causes peer `readexactly` to raise `IncompleteReadError`
- [x] `drain()` actually blocks above the high-water mark and releases below the low-water mark (test with explicit reads on the peer reader)
- [x] `server.close()` stops accepting; subsequent `open_connection` fails
- [x] `server.wait_closed()` awaits in-flight handler tasks to completion; does NOT cancel them
- [x] `writer.get_extra_info("peername")` / `("sockname")` return the virtual addresses
- [x] `ssl` parameter is accepted and logged at DEBUG, not raised

**Integration with the port:**

- [x] `InMemoryNetwork` is a full `INetwork` implementation (no unimplemented methods)
- [x] Shared instance between two consumers in the same test routes traffic correctly

**Regression guard:**

- [x] Canary test: intentionally importing `asyncio.start_server` in a test module triggers a collection failure with a message pointing to `INetwork`

**Quality gates:**

- [x] `ruff`, `pylint` (10.00/10), `mypy` all clean

## Findings of code review

No issues found. Review checked:

- Clean code: the module is organised top-down (socket info → reader → transport → writer → factory → server → network), each class has a single responsibility. Private helpers (`_MemorySocketInfo`, `_MemoryStreamReader`, `_MemoryTransport`, `_make_pair`) stay under one underscore since they are implementation details of the in-memory adapter.
- SOLID: `InMemoryNetwork` depends only on the `INetwork` abstraction it implements; flow-control state lives on `_MemoryTransport` so the writer and protocol need no private-attribute coupling. `_MemoryStreamReader` only adds peer-notification callbacks, keeping inheritance narrow.
- DRY/YAGNI: avoided building two separate drain mechanisms — the transport owns the single drain waiter and the writer's `drain()` delegates. `readuntil` is not overridden (no consumer uses it in pyleans today); the hooked read methods are the ones actually exercised.
- Strict typing: `mypy -p pyleans.net` is clean. One narrowly-scoped `# pylint: disable-next=too-many-public-methods` on `_MemoryTransport` is justified by its obligation to implement the full `asyncio.Transport` contract.
- Test coverage: 27 tests across 13 classes cover every acceptance criterion in the spec, including a subprocess-based end-to-end canary that proves `pytest --collect-only` fails when a test module imports `asyncio.start_server`.

## Findings of security review

No issues found. Review checked:

- No new system-boundary inputs — the simulator is test-only and never binds a real socket.
- Registry (`self._servers`) is keyed by `(host, port)` tuples; no user-controlled deserialisation, path handling, or subprocess invocation in production code. (The subprocess in the regression-guard canary test is scoped to the pytest harness itself and invokes `sys.executable` with a controlled argv.)
- No unbounded resource consumption: each transport tracks a bounded buffer counter, not the actual bytes (the StreamReader buffer holds bytes, but pyleans' protocol frames bound reads); `handler_tasks` is discarded on completion.
- Write-after-close raises `ConnectionResetError` immediately instead of silently dropping data.
- TLS is explicitly not simulated and is documented — no false sense of security.

## Summary of implementation

### Files created/modified

- **Created**: `src/pyleans/pyleans/net/memory_network.py` — `InMemoryNetwork`, `InMemoryServer`, `MemoryStreamWriter`, and internal helpers (`_MemorySocketInfo`, `_MemoryStreamReader`, `_MemoryTransport`, `_make_pair`).
- **Modified**: `src/pyleans/pyleans/net/__init__.py` — re-export `InMemoryNetwork`, `InMemoryServer`, `MemoryStreamWriter`.
- **Modified**: `src/pyleans/pyleans/__init__.py` — re-export `InMemoryNetwork` at the package root.
- **Modified**: `src/pyleans/test/conftest.py` — added `scan_for_forbidden_asyncio_net_calls` and the `pytest_collectstart` hook that fails collection for tests that bypass the port.
- **Created**: `src/pyleans/test/test_memory_network.py` — 27 unit tests covering the full acceptance matrix including an end-to-end regression-guard canary.

### Key implementation decisions

- **Drain state owned by the transport, not the protocol.** The spec sketch reaches into `StreamReaderProtocol._drain_waiter`/`_paused`, which is brittle and fails strict mypy (those attrs aren't in the typeshed). Instead, `_MemoryTransport` owns the single `_drain_waiter` future and exposes `await_drain()`. `MemoryStreamWriter.drain()` delegates to it, and reads on the peer's `_MemoryStreamReader` release the waiter when the buffered count drops below `low_water`.
- **`_MemoryStreamReader` is a true `asyncio.StreamReader` subclass.** Overriding `read`, `readexactly`, and `readline` is enough — `readuntil` isn't used by any pyleans consumer, so per YAGNI it isn't hooked. Tests that need it can still use it (backpressure would be slightly delayed, not broken).
- **`MemoryStreamWriter` subclasses `asyncio.StreamWriter`.** This keeps `INetwork.open_connection`'s return type exactly `tuple[StreamReader, StreamWriter]`, so every consumer that already expects those types (existing gateway + client code) works unchanged.
- **`_MemoryTransport` implements the full `asyncio.Transport` contract** including the `WriteTransport` and `ReadTransport` abstract methods (`set_write_buffer_limits`, `get_write_buffer_size`, `get_write_buffer_limits`, `pause_reading`, `resume_reading`, `is_reading`). Read-side methods are no-ops (there is no read side to pause in-memory); write-side methods mirror the adjustable water marks. This keeps pylint happy and makes the transport a drop-in for any asyncio code path that introspects it.
- **Regression guard uses AST, not substring matching.** `scan_for_forbidden_asyncio_net_calls` walks the module AST so docstring/comment/string-literal mentions of `asyncio.start_server` do not trigger false positives — essential because the adapter's own tests and docstrings mention the call by name.
- **Guard skips non-test modules.** The hook only fires for files named `test_*.py`, so the regression guard never interferes with production code (which is migrated separately in task-01-21 Phase B).

### Deviations from original design

- No spec-mandated `_SocketInfo` private type was introduced (see task-01-15 deviation). `SocketInfo` is the public Protocol consumed here.
- `InMemoryNetwork.__init__` accepts `high_water`, `low_water`, and `reader_limit` kwargs so tests can deterministically drive backpressure below asyncio's default thresholds — documented as test-tuning knobs, not part of the production default.
- The simulator exposes `active_addresses()` for test-time debugging. Trivial accessor, not part of the spec's API surface.

### Test coverage summary

27 tests across 13 classes:

- `TestRoundTrip` (2) — ordered byte delivery; one shared instance powering two consumers.
- `TestConnectionRefused` (2) — unbound address; address that was closed.
- `TestAddressInUse` (2) — EADDRINUSE on duplicate registration; re-use after close.
- `TestVirtualPortAllocation` (2) — monotonic ports ≥40000; per-instance counters independent.
- `TestSimulateReset` (2) — raises on next `write`/`drain`; unblocks an in-flight `drain`.
- `TestSimulatePeerClose` (1) — peer `readexactly` raises `IncompleteReadError`.
- `TestDrainBackpressure` (2) — blocks above high-water / releases below low-water; small writes return immediately.
- `TestServerLifecycle` (2) — `close()` stops accepting; `wait_closed()` awaits in-flight handlers without cancelling them.
- `TestExtraInfo` (2) — `peername`/`sockname` are virtual addresses on both sides; unknown key returns default.
- `TestSslParameter` (1) — `ssl` parameter is accepted and logged at DEBUG.
- `TestINetworkInterface` (2) — `InMemoryNetwork` IS a `INetwork`; re-exports from `pyleans.net` and `pyleans`.
- `TestInMemoryServerType` (1) — server IS a `NetworkServer` and `InMemoryServer`.
- `TestRegressionGuard` (6) — scanner detects direct calls and `from asyncio import …`; ignores correct `INetwork` usage and string/comment occurrences; end-to-end subprocess test proves `pytest --collect-only` fails when a test module violates the rule.
