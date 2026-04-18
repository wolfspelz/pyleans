# Task 01-15: `INetwork` Port and Asyncio Adapter

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-14-in-memory-streaming.md](task-01-14-in-memory-streaming.md) (previous Phase 1 task; no direct code dependency, ordered purely to land the port before its first consumer)

## References
- [adr-network-port-for-testability](../adr/adr-network-port-for-testability.md)
- [adr-provider-interfaces](../adr/adr-provider-interfaces.md)
- [adr-cluster-transport](../adr/adr-cluster-transport.md) (one layer above `INetwork`)
- [pyleans-transport.md](../architecture/pyleans-transport.md) -- §4 assumes a reliable stream abstraction

## Description

Define the `INetwork` port and its production `AsyncioNetwork` adapter — the TCP-I/O abstraction every subsequent networking component in pyleans consumes. Any code that would otherwise call `asyncio.start_server` or `asyncio.open_connection` directly goes through this port instead, so the in-memory simulator from [task-01-16](task-01-16-in-memory-network-simulator.md) can be swapped in at test time without the runtime layer noticing.

This task ships the port ABC and the production adapter. The matching in-memory simulator lands in [task-01-16](task-01-16-in-memory-network-simulator.md). The first consumers are [task-01-17 (Silo)](task-01-17-silo.md) — whose `GatewayListener` accepts a `network` parameter from day one — and [task-01-20 (Counter Client)](task-01-20-counter-client.md) for the `ClusterClient`. Phase 2's TCP mesh transport ([task-02-08](task-02-08-tcp-cluster-transport.md)) reuses the same port via `TransportOptions.network`.

Network-free Phase 1 tasks (01-01 through 01-14) don't touch the port. It is introduced here so that the very first time a Phase 1 component opens a socket, it does so through `INetwork`.

### Files to create

- `src/pyleans/pyleans/net/__init__.py` -- re-exports `INetwork`, `NetworkServer`, `AsyncioNetwork`
- `src/pyleans/pyleans/net/network.py` -- `INetwork` ABC, `NetworkServer` ABC
- `src/pyleans/pyleans/net/asyncio_network.py` -- production pass-through adapter
- `src/pyleans/pyleans/__init__.py` -- re-exports at the package root (additive to existing re-exports)
- `src/pyleans/test/test_asyncio_network.py` -- unit tests for the production adapter

### Design

Port ABC mirrors asyncio's native API one-to-one so the adapter surface is trivial and so that any future consumer can switch from `asyncio.start_server`/`open_connection` to `INetwork` with a one-symbol change:

```python
# net/network.py
import ssl
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from asyncio import StreamReader, StreamWriter


class NetworkServer(ABC):
    """Minimal subset of ``asyncio.Server`` every adapter implements.

    Covers exactly what pyleans needs: graceful close, wait-for-close, and
    port discovery via ``.sockets[0].getsockname()``. Extended only when a
    concrete consumer requires more.
    """

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    async def wait_closed(self) -> None: ...

    @property
    @abstractmethod
    def sockets(self) -> list["_SocketInfo"]: ...


class _SocketInfo(ABC):
    """Duck-compatible with the ``socket.socket`` objects returned by
    ``asyncio.Server.sockets`` -- just needs ``getsockname()``."""

    @abstractmethod
    def getsockname(self) -> tuple[str, int]: ...


ClientConnectedCallback = Callable[[StreamReader, StreamWriter], Awaitable[None]]


class INetwork(ABC):
    """Pluggable TCP-stream factory. Production uses AsyncioNetwork;
    tests use InMemoryNetwork (see task-01-16)."""

    @abstractmethod
    async def start_server(
        self,
        client_connected_cb: ClientConnectedCallback,
        host: str,
        port: int,
        *,
        ssl: ssl.SSLContext | None = None,
    ) -> NetworkServer: ...

    @abstractmethod
    async def open_connection(
        self,
        host: str,
        port: int,
        *,
        ssl: ssl.SSLContext | None = None,
    ) -> tuple[StreamReader, StreamWriter]: ...
```

Production adapter:

```python
# net/asyncio_network.py
import asyncio
from pyleans.net.network import INetwork, NetworkServer, ClientConnectedCallback


class AsyncioNetwork(INetwork):
    """Production default. Thin pass-through to asyncio."""

    async def start_server(
        self, client_connected_cb, host, port, *, ssl=None,
    ) -> NetworkServer:
        server = await asyncio.start_server(client_connected_cb, host, port, ssl=ssl)
        return _AsyncioNetworkServer(server)

    async def open_connection(self, host, port, *, ssl=None):
        return await asyncio.open_connection(host, port, ssl=ssl)


class _AsyncioNetworkServer(NetworkServer):
    def __init__(self, server: asyncio.Server) -> None:
        self._server = server

    def close(self) -> None: self._server.close()

    async def wait_closed(self) -> None: await self._server.wait_closed()

    @property
    def sockets(self): return list(self._server.sockets)
```

`asyncio.Server.sockets` returns real `socket.socket` objects whose `getsockname()` satisfies the `_SocketInfo` contract without a wrapper.

### Intended consumer pattern

Later tasks construct their components with an optional `network` parameter that defaults to `AsyncioNetwork()`. The lazy-default idiom keeps the adapter per-instance (no shared module-import singleton) so a future stateful adapter doesn't leak state between consumers:

```python
# Canonical usage — illustrated for GatewayListener in task-01-17 and
# for ClusterClient in task-01-20.
class Consumer:
    def __init__(self, *, network: INetwork | None = None) -> None:
        self._network = network or AsyncioNetwork()
```

### Scope

This task delivers the port, the asyncio adapter, and their unit tests. Nothing else in the codebase is modified: no networking consumer exists yet to be wired. The first consumer ([task-01-17 Silo](task-01-17-silo.md)) imports `INetwork` and `AsyncioNetwork` directly.

### Acceptance criteria

- [x] `INetwork`, `NetworkServer` are non-instantiable ABCs; abstract methods raise `TypeError` on direct instantiation
- [x] `AsyncioNetwork.start_server` returns a `NetworkServer` whose `.sockets[0].getsockname()` reports the actual bound port (including when `port=0` is passed)
- [x] `AsyncioNetwork.open_connection` returns a tuple `(StreamReader, StreamWriter)` indistinguishable in type from `asyncio.open_connection`
- [x] `NetworkServer.close()` + `wait_closed()` cleanly close the underlying `asyncio.Server`
- [x] `ssl=None` passes through without error; `ssl=<context>` configures the underlying asyncio call (smoke test only — TLS correctness is not in scope)
- [x] `INetwork`, `AsyncioNetwork`, `NetworkServer` are re-exported from `pyleans.net` and from `pyleans`
- [x] Unit tests in `src/pyleans/test/test_asyncio_network.py` cover: round-trip connect + send + close, `port=0` discovery, `ssl=None` passthrough, server lifecycle, concurrent client connections to one server
- [x] `ruff`, `pylint` (10.00/10), `mypy` all clean

## Findings of code review

No issues found. Review checked:

- Clean code: `network.py` is the port definition and nothing else; `asyncio_network.py` is a thin pass-through with no logic beyond wrapping. Single responsibility per module.
- SOLID: `INetwork` is a narrow abstraction (two methods) that every consumer can substitute; `NetworkServer` is separated so adapters can implement their own server type; `AsyncioNetwork` depends only on the port abstraction it implements.
- DRY/YAGNI: only the asyncio `start_server` / `open_connection` surface is modeled — nothing speculative. `SocketInfo` is a `Protocol`, not an ABC, so real `socket.socket` objects satisfy it structurally with no wrapper class.
- Type hints: all public APIs fully typed; strict mypy passes on `pyleans.net`. `SocketInfo.getsockname()` is typed as `Any` with a docstring explaining the IPv4/IPv6 return-shape divergence from the stdlib.
- Test quality: 12 tests across 5 classes cover ABC non-instantiability, round-trip I/O, `port=0` discovery, stream-pair types, server lifecycle, `ssl=None` pass-through, `ssl=<context>` propagation, and concurrent connections to one server.

## Findings of security review

No issues found. Review checked:

- No new system-boundary inputs; the port is internal plumbing and forwards arguments unchanged to asyncio.
- `ssl` parameter is passed through to the stdlib — no custom TLS handling or unsafe defaults introduced.
- No deserialization, path handling, file access, or subprocess invocation.
- No unbounded resource consumption: the adapter owns no buffers or queues; lifetime is bound to the server object returned to the caller.
- `_AsyncioNetworkServer` is intentionally private and exposes only the subset of `asyncio.Server` that pyleans depends on — reduced surface area.

## Summary of implementation

### Files created/modified

- **Created**: `src/pyleans/pyleans/net/__init__.py` — re-exports `INetwork`, `NetworkServer`, `AsyncioNetwork`, `ClientConnectedCallback`, `SocketInfo`.
- **Created**: `src/pyleans/pyleans/net/network.py` — `INetwork` and `NetworkServer` ABCs plus the `SocketInfo` Protocol and `ClientConnectedCallback` alias.
- **Created**: `src/pyleans/pyleans/net/asyncio_network.py` — `AsyncioNetwork` and the private `_AsyncioNetworkServer` wrapper.
- **Modified**: `src/pyleans/pyleans/__init__.py` — additive re-exports of `INetwork`, `NetworkServer`, `AsyncioNetwork`.
- **Created**: `src/pyleans/test/test_asyncio_network.py` — 12 unit tests covering every acceptance criterion.

### Key implementation decisions

- Chose `Protocol` for `SocketInfo` (not an ABC) so real `socket.socket` objects returned by `asyncio.Server.sockets` satisfy it structurally without a wrapper — keeps `AsyncioNetwork` a true zero-wrapping pass-through on the sockets property.
- `SocketInfo.getsockname()` returns `Any` (with a docstring) to cover IPv4 `(str, int)` and IPv6 `(str, int, int, int)` without forcing callers into a union. Callers only read `[1]` for the port, which is `int` in both shapes.
- Renamed the spec's `_SocketInfo` to `SocketInfo` (public) because it appears in the public return type of `NetworkServer.sockets`; leading-underscore names in public signatures are awkward. Non-breaking relative to the spec.
- `_AsyncioNetworkServer` is kept private — not re-exported — since it's an implementation detail of `AsyncioNetwork`.

### Deviations from original design

- None of substance. The only rename (`_SocketInfo` → `SocketInfo`) is a public-API hygiene choice consistent with the spec's intent.

### Test coverage summary

12 tests across 5 test classes:

- `TestAbstractBaseClasses` — `INetwork` and `NetworkServer` both raise `TypeError` on direct instantiation.
- `TestReExports` — both `pyleans.net` and `pyleans` expose `INetwork`, `AsyncioNetwork`, `NetworkServer`.
- `TestStartServerAndOpenConnection` — end-to-end round trip (bytes in, bytes out), `port=0` assigns a real OS port, `open_connection` returns real `StreamReader`/`StreamWriter`.
- `TestServerLifecycle` — `close()` + `wait_closed()` shut down cleanly and subsequent connects fail; `sockets` returns a populated list with a valid `(host, port)` tuple.
- `TestSslParameter` — `ssl=None` passes through; `ssl=<context>` is propagated to the stdlib (asserted by observing asyncio/SSL layer reactions, without depending on TLS correctness).
- `TestConcurrentConnections` — five concurrent clients all handled by one server simultaneously (peak concurrency verified).
