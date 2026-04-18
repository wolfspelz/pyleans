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

- [ ] `INetwork`, `NetworkServer` are non-instantiable ABCs; abstract methods raise `TypeError` on direct instantiation
- [ ] `AsyncioNetwork.start_server` returns a `NetworkServer` whose `.sockets[0].getsockname()` reports the actual bound port (including when `port=0` is passed)
- [ ] `AsyncioNetwork.open_connection` returns a tuple `(StreamReader, StreamWriter)` indistinguishable in type from `asyncio.open_connection`
- [ ] `NetworkServer.close()` + `wait_closed()` cleanly close the underlying `asyncio.Server`
- [ ] `ssl=None` passes through without error; `ssl=<context>` configures the underlying asyncio call (smoke test only — TLS correctness is not in scope)
- [ ] `INetwork`, `AsyncioNetwork`, `NetworkServer` are re-exported from `pyleans.net` and from `pyleans`
- [ ] Unit tests in `src/pyleans/test/test_asyncio_network.py` cover: round-trip connect + send + close, `port=0` discovery, `ssl=None` passthrough, server lifecycle, concurrent client connections to one server
- [ ] `ruff`, `pylint` (10.00/10), `mypy` all clean

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
