# Pyleans Implementation Plan

A Python implementation of Orleans-style virtual actors. This is a living document --
updated as we iterate on design decisions.

## Status: All design decisions resolved -- Ready for implementation

---

## 1. Scope: What to Include in the Proof of Concept

### Included (PoC)

| Orleans Concept | Pyleans PoC | Notes |
|---|---|---|
| Grains (virtual actors) | Yes | Core concept |
| Grain identity (type + key) | Yes | String keys only initially |
| Grain lifecycle (activate/deactivate) | Yes | on_activate, on_deactivate hooks |
| Single-threaded turn-based execution | Yes | asyncio, one coroutine per grain at a time |
| Grain references (proxies) | Yes | Location-transparent method calls |
| Grain interfaces | No separate interface | `@grain` decorator on class, proxy via `__getattr__` |
| Silo (grain host process) | Yes | asyncio-based Python process |
| Cluster (multiple silos) | Yes | Minimum 2 silos to prove distribution |
| Grain directory | Yes | Consistent hash ring, eventually consistent (pre-Orleans 9.0) |
| Membership provider | Yes | Pluggable, file-based default |
| Storage provider | Yes | Pluggable, JSON-file default |
| Streaming provider | Yes (basic) | Pluggable, in-memory default |
| Placement strategies | Minimal | Random + prefer-local only |
| TCP mesh transport | Yes | Custom asyncio TCP, as designed in pyleans-transport.md |
| Idle collection | Yes | Deactivate after timeout |
| Timers | Yes | In-grain periodic callbacks |
| Dependency injection | Yes | `dependency-injector`, constructor injection like Orleans |
| Client gateway | Yes | External clients connect via ClusterClient over gateway protocol |

### Excluded from PoC

| Feature | Reason |
|---|---|
| Transactions | Explicitly excluded per requirements |
| Reentrancy / interleaving | Adds complexity, not needed for PoC |
| Reminders (persistent timers) | Requires reminder storage, defer to later |
| Stateless workers | Optimization, not core |
| Grain observers | Can use streaming instead |
| Grain call filters | Interceptors are a refinement |
| Rolling upgrades | Operational concern, not PoC |
| MQTT/WebSocket gateway | Phase 2 transport |

---

## 2. Key Design Decisions

### Decision 1: Grain Interfaces -- Separate or Not?

**C# approach**: Orleans requires a separate `IGrainInterface` (C# interface) and a
`GrainClass` (implementation). The interface is used to generate strongly-typed proxies.
This is idiomatic C# and enables compile-time type checking.

```csharp
// C# Orleans
public interface IPlayerGrain : IGrainWithStringKey
{
    Task<string> GetName();
    Task SetName(string name);
}

public class PlayerGrain : Grain, IPlayerGrain
{
    private string _name;
    public Task<string> GetName() => Task.FromResult(_name);
    public Task SetName(string name) { _name = name; return Task.CompletedTask; }
}
```

**Python options**:

**Option A -- Decorated class only (no separate interface)**:
```python
@grain
class PlayerGrain:
    def __init__(self):
        self.name = ""

    async def get_name(self) -> str:
        return self.name

    async def set_name(self, name: str) -> None:
        self.name = name
```
- Pros: Minimal boilerplate, Pythonic, fast to write.
- Cons: No explicit contract. The proxy must introspect the class or use `__getattr__`.
  Type checkers can't verify grain calls at the call site.

**Option B -- ABC as interface, class as implementation**:
```python
class IPlayerGrain(GrainInterface):
    async def get_name(self) -> str: ...
    async def set_name(self, name: str) -> None: ...

@grain
class PlayerGrain(Grain, IPlayerGrain):
    def __init__(self):
        self.name = ""

    async def get_name(self) -> str:
        return self.name

    async def set_name(self, name: str) -> None:
        self.name = name
```
- Pros: Explicit contract. Proxy can be typed as `IPlayerGrain`. Type checkers work.
  Closer to Orleans. Enables generating typed stubs.
- Cons: More boilerplate. Two things to maintain.

**Option C -- Protocol-based (structural typing)**:
```python
class IPlayerGrain(Protocol):
    async def get_name(self) -> str: ...
    async def set_name(self, name: str) -> None: ...

@grain
class PlayerGrain:
    # No explicit inheritance needed -- just implement the methods
    ...
```
- Pros: Pythonic structural typing. Type checkers can verify without inheritance.
- Cons: Protocol is a type-checking concept, not a runtime one. Harder to use
  for proxy generation at runtime.

**DECIDED: Option A -- Decorated class only.**
The `@grain` decorator registers the class and its public async methods as the grain
interface. Proxy objects use `__getattr__` to forward calls. This gets us running fast.
If we later need typed proxies for larger projects, we add optional ABC-based interfaces.

---

### Decision 2: Concurrency Model -- asyncio and the GIL

**The problem**: Orleans runs grains in a single-threaded turn-based model. Each grain
processes one message at a time. But a silo hosts thousands of grains concurrently.
In C#/.NET, this is handled by the Task scheduler and async/await.

**Python's situation**:
- **GIL**: Only one thread executes Python bytecode at a time. This is actually
  *helpful* -- it gives us the single-threaded guarantee for free within a process.
- **asyncio**: Python's built-in cooperative concurrency. Perfect fit for the
  turn-based model. Each grain method is a coroutine. While one grain awaits (e.g.,
  calling another grain, reading storage), other grains can run.
- **Multi-core**: A single Python process uses one core. For multi-core, run
  multiple silo processes (one per core or a subset). This is exactly the Orleans
  model -- multiple silos on the same machine.

**Design**:
```
One silo = one Python process = one asyncio event loop

Grain activation:
  - Each grain has a dedicated asyncio.Queue for incoming messages
  - A worker coroutine drains the queue one message at a time
  - While processing, the grain may `await` (yielding to other grains)
  - No other message for THIS grain runs until the current one completes
  - But OTHER grains on the same silo run concurrently via asyncio
```

**This is settled**: asyncio is the right answer. No threads needed inside a silo.
The GIL is our friend here. Multi-core = multiple silo processes.

**Multi-core**: One silo = one process = one core. Want multi-core? Run multiple silos.
No built-in multi-process wrapper -- the operator starts N silo processes on different
ports (via shell, systemd, k8s, etc.). They find each other via the membership table.

Note: Python 3.13+ has experimental free-threaded mode (no GIL). We keep this in mind
but don't rely on it. Our asyncio design works with or without the GIL.

---

### Decision 3: Serialization

**C# Orleans**: Uses a custom code-generated serializer with `[GenerateSerializer]` and
`[Id(n)]` field tags. Supports versioning, object graph references, and inheritance.

**Python options**:

| Format | Pros | Cons |
|---|---|---|
| JSON (`json` / `orjson`) | Human-readable, universal, no schema | Slow for large payloads, no binary |
| MessagePack (`msgpack`) | Fast, compact, cross-language | Not human-readable |
| pickle | Zero-effort for Python objects | Security risk, Python-only, version-fragile |
| Protocol Buffers | Schema, cross-language, fast | Requires `.proto` compilation step |

**DECIDED: JSON for PoC** (using `dataclasses` + `orjson` for speed).
The serialization layer is behind an interface, so we can swap in msgpack later.

Grain state and method arguments must be serializable. We enforce this by convention:
grain state should be a `@dataclass`, method arguments should be JSON-serializable types.

```python
@dataclass
class PlayerState:
    name: str = ""
    level: int = 1
    inventory: list[str] = field(default_factory=list)

@grain(state_type=PlayerState, storage="default")
class PlayerGrain:
    state: PlayerState  # auto-loaded on activation, auto-typed

    async def get_name(self) -> str:
        return self.state.name
```

---

### Decision 4: Dependency Injection

**C# Orleans**: Full .NET DI container. ALL services -- framework and user -- are
constructor-injected. There is no special context bag. `IGrainFactory`, `ILogger`,
`IPersistentState<T>`, and user services all come through the constructor.

**DECIDED: Match Orleans -- all-DI via `dependency-injector`, no context object.**

Using the `dependency-injector` package, framework and user services are injected
identically through the grain constructor:

```python
from abc import ABC, abstractmethod
from dependency_injector import containers, providers
from dependency_injector.wiring import inject, Provide


# --- App-specific service interface (defined by the application) ---

class IEmailService(ABC):
    """Application-defined interface for sending emails."""
    @abstractmethod
    async def send(self, to: str, subject: str, body: str) -> None: ...

class SmtpEmailService(IEmailService):
    """Concrete implementation using SMTP."""
    def __init__(self, smtp_host: str, api_key: str):
        self.smtp_host = smtp_host
        self.api_key = api_key

    async def send(self, to: str, subject: str, body: str) -> None:
        # ... actual SMTP sending ...
        pass


# --- DI Container (configured at silo startup) ---

class SiloContainer(containers.DeclarativeContainer):
    config = providers.Configuration()

    # Framework services (provided by pyleans)
    grain_factory = providers.Singleton(GrainFactory)
    timer_registry = providers.Singleton(TimerRegistry)
    stream_manager = providers.Singleton(StreamManager)
    logger = providers.Singleton(logging.getLogger, "pyleans")

    # App-specific services (provided by the application)
    email_service = providers.Singleton(
        SmtpEmailService,
        smtp_host=config.smtp_host,
        api_key=config.email_api_key,
    )


# --- Grain using both framework and app services ---

@grain(state_type=PlayerState, storage="default")
class PlayerGrain:
    @inject
    def __init__(self,
                 grain_factory: GrainFactory = Provide[SiloContainer.grain_factory],
                 logger: Logger = Provide[SiloContainer.logger],
                 email: IEmailService = Provide[SiloContainer.email_service]):
        self.grain_factory = grain_factory
        self.logger = logger
        self.email = email  # injected as IEmailService, resolved to SmtpEmailService

    async def on_activate(self):
        self.logger.info(f"Player {self.identity} activated")

    async def get_name(self) -> str:
        return self.state.name

    async def send_welcome(self) -> None:
        await self.email.send(
            to=self.state.email,
            subject="Welcome!",
            body=f"Welcome {self.state.name}!",
        )

    async def join_room(self, room_key: str) -> None:
        room = self.grain_factory.get_grain(ChatRoomGrain, room_key)
        await room.add_member(self.identity.key)
```

**What gets injected**:

| Service | Type | How |
|---|---|---|
| `GrainFactory` | Get references to other grains | `Provide[SiloContainer.grain_factory]` |
| `Logger` | Logging | `Provide[SiloContainer.logger]` |
| `TimerRegistry` | Register grain timers | `Provide[SiloContainer.timer_registry]` |
| `StreamManager` | Get stream references | `Provide[SiloContainer.stream_manager]` |
| `IEmailService` | App-defined ABC, resolved to impl | `Provide[SiloContainer.email_service]` |
| Other user services | Whatever users register | `Provide[SiloContainer.xxx]` |

**Grain identity and state** are provided by the `@grain` decorator / runtime:
- `self.identity` -- set by the runtime before `__init__` (via the decorator)
- `self.state` -- loaded from storage on activation (configured via `@grain(state_type=...)`)

**Testing**: Clean -- just pass mocks to the constructor. The app interface
makes it natural to swap implementations:
```python
class MockEmailService(IEmailService):
    async def send(self, to, subject, body): 
        self.last_sent = (to, subject, body)

grain = PlayerGrain(
    grain_factory=mock_factory,
    logger=mock_logger,
    email=MockEmailService(),
)
```

---

### Decision 5: Provider Interfaces -- Kept Minimal

All providers follow the same pattern: an ABC with the minimum required methods.

**Storage Provider**:
```python
class StorageProvider(ABC):
    @abstractmethod
    async def read(self, grain_type: str, grain_key: str) -> tuple[Any, str | None]:
        """Returns (state_dict, etag). Returns ({}, None) if not found."""
        ...

    @abstractmethod
    async def write(self, grain_type: str, grain_key: str,
                    state: Any, expected_etag: str | None) -> str:
        """Writes state, returns new etag. Raises on etag mismatch."""
        ...

    @abstractmethod
    async def clear(self, grain_type: str, grain_key: str,
                    expected_etag: str | None) -> None:
        """Deletes grain state."""
        ...
```

**Membership Provider**:
```python
class MembershipProvider(ABC):
    @abstractmethod
    async def register_silo(self, silo: SiloInfo) -> None: ...

    @abstractmethod
    async def unregister_silo(self, silo_id: str) -> None: ...

    @abstractmethod
    async def get_active_silos(self) -> list[SiloInfo]: ...

    @abstractmethod
    async def heartbeat(self, silo_id: str) -> None: ...
```

**Stream Provider**:
```python
class StreamProvider(ABC):
    @abstractmethod
    async def publish(self, stream_ns: str, stream_key: str,
                      event: Any) -> None: ...

    @abstractmethod
    async def subscribe(self, stream_ns: str, stream_key: str,
                        callback: Callable) -> StreamSubscription: ...

    @abstractmethod
    async def unsubscribe(self, subscription: StreamSubscription) -> None: ...
```

Each provider gets a simple default implementation:
- Storage: `JsonFileStorageProvider` (one JSON file per grain)
- Membership: `FileMembershipProvider` (shared JSON file or directory)
- Streaming: `InMemoryStreamProvider` (asyncio queues, single-silo only)

---

## 3. Architecture Overview

```
  +-----------+
  | Web Client|
  +-----+-----+
        |  HTTP
  +-----v-----------+
  | Web Server      |     (separate process, e.g. FastAPI)
  | (ClusterClient) |
  +-----+-----------+
        |  gateway protocol (persistent connection)
        |
        |     +-------------+  TCP mesh  +-------------+
        +---->|    Silo A    |<---------->|    Silo B    |
              |              |            |              |
              | +----------+ |            | +----------+ |
              | | Grain    | |            | | Grain    | |
              | | (User)   | |            | | (Room)   | |
              | +----------+ |            | +----------+ |
              |              |            |              |
              | +----------+ |            | +----------+ |
              | | Runtime  | |            | | Runtime  | |
              | +----------+ |            | +----------+ |
              |              |            |              |
              | +----------+ |            | +----------+ |
              | | Providers| |            | | Providers| |
              | +----------+ |            | +----------+ |
              |              |            |              |
              | +----------+ |            | +----------+ |
              | | Transport| |            | | Transport| |
              | | - Mesh   | |            | | - Mesh   | |
              | | - Gateway| |            | | - Gateway| |
              | +----------+ |            | +----------+ |
              +--------------+            +--------------+
```

**Key design principle**: The silo is a standalone process. No HTTP server runs
inside the silo. External clients (web servers, CLI tools, other services) connect
via `ClusterClient` using the gateway protocol over a persistent connection.
A FastAPI or other web API is a separate service that uses `ClusterClient`. The
silo _can_ be co-hosted with a web server (via `start_background()`), but this
is an advanced pattern, not the default.

---

## 4. Package Structure

Uses **pip + venv** with editable installs. See CLAUDE.md for the full directory tree.

```
pyproject.toml                   # workspace root
pyleans/                         # framework package
  pyproject.toml
  pyleans/                       # importable: import pyleans
    server/                      # silo runtime (heavy)
    client/                      # lightweight client
    providers/                   # provider ABCs (ports)
  test/
counter-app/                     # sample silo app (depends on pyleans)
  pyproject.toml
  counter/
    grains.py                    # CounterGrain
    main.py                      # Standalone silo
  test/
counter-client/              # CLI client tool
```

**Package manager**: `pip` with `venv` and editable installs (`pip install -e`).
`pyproject.toml` with `[project]` metadata, `[build-system]` using hatchling.

**Dependencies (pyleans)**:
- `dependency-injector` -- DI container (constructor injection for grains)
- `orjson` -- fast JSON serialization
- `pyyaml` -- YAML membership provider

**No optional web dependencies**: FastAPI or other web frameworks are not pyleans
dependencies. A web API is a separate service that uses `pyleans.client`.

**Dev dependencies** (workspace root): `pytest`, `pytest-asyncio`, `ruff`, `mypy`.

---

## 5. Implementation Phases

### Phase 1: Single Silo -- Dev Mode (in-process, no networking)

Everything runs in one Python process. Like Orleans' `UseLocalhostClustering()`.

1. `@grain` decorator with `state_type` and `storage` params
2. `SiloContainer` with `dependency-injector` (GrainFactory, TimerRegistry, etc.)
3. Constructor injection for grains (framework + user services)
4. Grain activation / deactivation lifecycle (`on_activate`, `on_deactivate`)
5. `GrainRef` proxy with `__getattr__` forwarding
6. In-memory grain directory (dict, single-silo -- no hashing needed yet)
7. Turn-based scheduler (asyncio queue per grain)
8. `JsonFileStorageProvider`
9. Grain state via `self.state` (dataclass, loaded on activation, `orjson` serialization)
10. Idle collection (deactivate after timeout)
11. Grain timers
12. Counter example: standalone silo + CLI client via gateway protocol

**Milestone**: A standalone silo hosting a counter grain that persists to a JSON file,
with a CLI client connecting via ClusterClient and the gateway protocol.

### Phase 2: Multi-Silo Cluster

Add networking and distribution.

1. `SiloAddress`, `GrainId` identity types
2. TCP mesh transport (from pyleans-transport.md design)
3. `FileMembershipProvider`
4. Consistent hash ring with virtual nodes (30 per silo)
5. Distributed grain directory partitioned across silos (eventually consistent)
6. Local directory cache with invalidation on membership change
7. Crash recovery: new partition owners query all silos to rebuild
8. Remote grain calls via transport
9. Silo startup/shutdown lifecycle
10. Placement strategies: random + prefer-local

**Milestone**: Two silo processes on localhost, a grain call from silo A executes on silo B.

### Phase 3: Streaming and Refinement

1. `InMemoryStreamProvider` (single-silo)
2. Stream subscriptions from grains
3. Multi-silo streaming (over transport)
4. Web server gateway for external clients
5. Basic placement strategies (random, prefer-local)

**Milestone**: Chat example with user grains and room grains exchanging messages via streams.

### Phase 4: Production Hardening (post-PoC)

- Redis/etcd membership provider
- Redis/S3 storage provider
- MQTT gateway transport
- Reentrancy support
- Reminders
- Metrics and observability
- Documentation and packaging for PyPI

---

## 6. Decisions Summary

All design decisions have been resolved:

| # | Decision | Choice |
|---|---|---|
| 1 | Grain interfaces | Decorated class only (`@grain`), no separate ABC. Proxy via `__getattr__`. |
| 2 | Concurrency model | asyncio, one event loop per silo process. Multi-core = run multiple silos. |
| 3 | Serialization | JSON via `dataclasses` + `orjson`. Pluggable interface for future swap. |
| 4 | Dependency injection | `dependency-injector` package. Constructor injection for all services (framework + user), matching Orleans. No context object. |
| 5 | Provider interfaces | Minimal ABCs: `StorageProvider`, `MembershipProvider`, `StreamProvider`. |
| 6 | Grain directory | Consistent hash ring with virtual nodes, eventually consistent (pre-Orleans 9.0). Strong consistency deferred to post-PoC. |
| 7 | State declaration | Explicit in decorator: `@grain(state_type=PlayerState, storage="default")`. No state_type = stateless grain. |
| 8 | Dev mode | Yes, single-silo mode like Orleans' `UseLocalhostClustering()`. Phase 1 delivers this. |
| 9 | Library vs CLI | Library only. User writes `main.py`, creates `Silo`, calls `silo.start()`. No CLI. |
| 10 | Package split | One package (`pyleans`), two entry points: `pyleans.server` (silo) and `pyleans.client` (lightweight). |

---

## 7. Resolved Questions

- **Minimum Python**: 3.12+
- **Pyleans is a library, not a CLI.** The user writes their own `main.py` that
  creates and starts a silo. This gives full control over DI setup, co-hosting
  with FastAPI, and custom startup logic.
- **One package, two entry points** (`Split A`): `pip install pyleans` gets everything.
  `from pyleans.server` for silo code, `from pyleans.client` for lightweight client.
  Client module avoids importing the heavy silo runtime.
- **Dashboard/admin UI**: Post-PoC.

### Silo usage (library, no CLI)

```python
# my_app/main.py
import asyncio
from pyleans.server import Silo
from my_grains import PlayerGrain, RoomGrain
from my_container import AppContainer

silo = Silo(
    port=11111,
    grains=[PlayerGrain, RoomGrain],
    container=AppContainer(),
)
asyncio.run(silo.start())
```

```bash
# Run it like any Python script
python my_app/main.py

# Or run multiple silos for multi-core
python my_app/main.py --port 11111 &
python my_app/main.py --port 11112 &
```

### Client usage (lightweight, no silo overhead)

```python
# web_server/api.py -- e.g. a FastAPI app that calls grains
from pyleans.client import ClusterClient

client = ClusterClient(gateways=["localhost:30000"])
await client.connect()

player = client.get_grain(PlayerGrain, "player-42")
name = await player.get_name()
```
