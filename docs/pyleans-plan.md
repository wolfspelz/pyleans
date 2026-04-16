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
| Grain identity (type + key) | Yes | String keys only (no guid/int/compound keys) |
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
| Guid/integer/compound grain keys | String keys cover all use cases; Orleans encodes all key types as strings internally anyway |

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

**Grain identity and state** are provided by the `Grain[TState]` base class / runtime:
- `self.identity` -- set by the runtime during activation
- `self.state` -- loaded from storage on activation (configured via `@grain(state_type=...)`)
- `self.write_state()`, `self.clear_state()`, `self.deactivate_on_idle()` -- bound by runtime
- See Decision 11 for the `Grain[TState]` base class that provides these

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

### Decision 11: Grain Base Class -- `Grain[TState]`

**The problem**: Every stateful grain must declare 5 identical runtime-bound attributes
(`identity`, `state`, `write_state`, `clear_state`, `deactivate_on_idle`) plus their
imports. This is pure boilerplate that adds noise and violates DRY.

**C# Orleans**: Grains inherit from `Grain<TState>`, which provides `State`, `WriteStateAsync()`,
`ClearStateAsync()`, and `DeactivateOnIdle()`. Users never declare these themselves.
Note: Orleans also parameterizes by key type (`IGrainWithStringKey`, `IGrainWithGuidKey`, etc.),
but pyleans uses string keys exclusively (see Excluded table), so `Grain[TState]` has only
one type parameter.

**Current pyleans approach** (Phase 1, to be refactored):
```python
@grain(state_type=CounterState, storage="default")
class CounterGrain:
    # 5 lines of identical boilerplate in every stateful grain
    identity: GrainId
    state: CounterState
    write_state: Callable[[], Awaitable[None]]
    clear_state: Callable[[], Awaitable[None]]
    deactivate_on_idle: Callable[[], None]

    async def get_value(self) -> int:
        return self.state.value
```

**DECIDED: Introduce a `Grain[TState]` generic base class.**

```python
# pyleans/grain_base.py
class Grain(Generic[TState]):
    """Base class for all grains. Provides runtime-bound attributes."""
    identity: GrainId
    state: TState

    async def write_state(self) -> None:
        """Persist the current state. Bound by runtime during activation."""
        raise GrainActivationError("write_state not bound -- grain not activated")

    async def clear_state(self) -> None:
        """Clear persisted state. Bound by runtime during activation."""
        raise GrainActivationError("clear_state not bound -- grain not activated")

    def deactivate_on_idle(self) -> None:
        """Request deactivation after current turn. Bound by runtime during activation."""
        raise GrainActivationError("deactivate_on_idle not bound -- grain not activated")
```

After refactoring, grains become:
```python
@grain(state_type=CounterState, storage="default")
class CounterGrain(Grain[CounterState]):
    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.write_state()
        return self.state.value
```

Stateless grains may optionally inherit `Grain[None]` if they need `identity` or
`deactivate_on_idle`, but are not required to.

**Implementation approach**: The base class provides stub methods that raise if called
before activation. The runtime overrides them with `setattr()` during activation
(same mechanism as today). This is the minimal change to the existing runtime.

**Bonus**: `state_type` can be inferred from the generic type argument, making
`@grain(storage="default")` sufficient — the `state_type` parameter becomes optional
for grains that inherit `Grain[TState]`.

---

### Decision 12: Naming Convention -- Follow Orleans, Python Case

**Rule**: pyleans method and property names follow Orleans naming but apply Python
conventions:

1. **snake_case** instead of PascalCase (standard Python).
2. **No `Async` suffix** on async methods — Python's `async def` already marks them.
3. **Same semantics and intent** as the Orleans equivalent.

**Mapping**:

| Orleans C# | pyleans Python | Notes |
|---|---|---|
| `State` (property) | `state` | snake_case |
| `WriteStateAsync()` | `write_state()` | no Async suffix |
| `ReadStateAsync()` | *(auto on activation)* | not exposed; same as Orleans default |
| `ClearStateAsync()` | `clear_state()` | no Async suffix |
| `DeactivateOnIdle()` | `deactivate_on_idle()` | snake_case |
| `OnActivateAsync()` | `on_activate()` | snake_case, no Async |
| `OnDeactivateAsync()` | `on_deactivate()` | snake_case, no Async |
| `GetPrimaryKeyString()` | `identity` (GrainId) | simplified — single key type |
| `GrainFactory.GetGrain<T>(key)` | `grain_factory.get_grain(T, key)` | snake_case |
| `RegisterTimer()` | `register_timer()` | snake_case |

This convention applies to all public API surfaces. Internal implementation names
follow standard Python conventions without requiring Orleans alignment.

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
      string_cache_grain.py      # system grain: StringCacheGrain
      grains.py                  # system_grains() helper
    client/                      # lightweight client
    gateway/                     # TCP gateway protocol
    providers/                   # provider ABCs (ports)
  test/
counter_app/                     # sample silo app (top-level module)
  counter_grain.py               # one file per grain
  main.py                        # Standalone silo
  __main__.py                    # python -m counter_app
  test/
counter_client/                  # sample CLI client (top-level module)
  main.py                        # CLI entry point
  __main__.py                    # python -m counter_client
  test/
```

### Naming convention

`pyleans` is a pip-installable package (`pip install -e pyleans`). The sample apps
(`counter_app`, `counter_client`) are top-level Python modules — no pip install
needed, just run from the project root.

| Module | Type | Run with |
|---|---|---|
| `pyleans` | Framework (pip-installed) | Library, not runnable |
| `counter_app` | Sample app (top-level module) | `python -m counter_app` |
| `counter_client` | Sample CLI (top-level module) | `python -m counter_client` |

### One grain per file

Every grain class lives in its own file, named after the grain in snake_case:
`CounterGrain` → `counter_grain.py`, `StringCacheGrain` → `string_cache_grain.py`.

This applies to both framework-provided grains (in `pyleans/server/`) and
application grains (in user packages like `counter_app/`). The grain's state
dataclass lives in the same file as the grain.

Test-only grains (defined inside test files) are exempt from this rule.

### Running the applications

All applications are run as Python modules. There are no installed console
scripts -- everything uses `python -m`.

```bash
# Install the framework in editable mode
pip install -e pyleans

# Terminal 1: start the silo (blocks, Ctrl+C to stop)
python -m counter_app

# Terminal 2: use the CLI client
python -m counter_client get my-counter
python -m counter_client inc my-counter
python -m counter_client set my-counter 42
python -m counter_client get my-counter --gateway localhost:30000
```

The silo listens on gateway port 30000 (configurable). State persists to
`./data/storage/` and membership to `./data/membership.yaml` relative to the
working directory.

**Package manager**: `pip` with `venv` and editable installs (`pip install -e`).
`pyproject.toml` with `[project]` metadata, `[build-system]` using hatchling.
No `[project.scripts]` -- all entry points are `__main__.py` modules.

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

**Milestone**: `python -m counter_app` runs a standalone silo hosting a counter grain
that persists to a JSON file. `python -m counter_client` connects via ClusterClient
and the TCP gateway protocol.

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
| 11 | Grain base class | `Grain[TState]` generic base class for runtime-bound attributes. Eliminates per-grain boilerplate. |
| 12 | Naming convention | Follow Orleans names in snake_case. No `Async` suffix — `async def` is sufficient. |

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
- **Grain base class**: See Decision 11.

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
