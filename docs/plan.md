# Pyleans Implementation Plan

A Python implementation of Orleans-style virtual actors. This is a living document --
updated as we iterate on design decisions.

## Status: All design decisions resolved -- Ready for implementation

## Document map

- **This file** — program overview, scope, phase roadmap, package structure.
- [docs/adr/](adr/) — Architecture Decision Records (MADR format), one decision per file.
- [docs/architecture/](architecture/) — long-form architecture specs (e.g. `pyleans-transport.md`).
- [docs/tasks/](tasks/) — per-task implementation specs for Phase 1.
- [docs/orleans-architecture/](orleans-architecture/) — Orleans reference material used while designing pyleans.
- [docs/orleans-sample/](orleans-sample/) — the Orleans C# sample app (reference implementation).
- [docs/papers/](papers/) — background papers and inspiration material.
- [CLAUDE.md](../CLAUDE.md) — coding standards and project rules for contributors (human or AI).

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
| TCP mesh transport | Yes | Custom asyncio TCP, as designed in [architecture/pyleans-transport.md](architecture/pyleans-transport.md) |
| Idle collection | Yes | Deactivate after timeout |
| Timers | Yes | In-grain periodic callbacks |
| Dependency injection | Yes | `injector` (Guice-style), type-hint constructor injection |
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
| MQTT/WebSocket gateway | Phase 4 transport |
| Guid/integer/compound grain keys | String keys cover all use cases; Orleans encodes all key types as strings internally anyway |
| Dashboard / admin UI | Post-PoC |

---

## 2. Design Reference

Design decisions are maintained as individual Architecture Decision Records in
[adr/](adr/). Each ADR is self-contained: context, options considered, decision,
consequences. See [adr/README.md](adr/README.md) for the full index.

**Grain model and runtime**
- [Grain Base Class `Grain[TState]`](adr/adr-grain-base-class.md)
- [Grain Interfaces via `@grain` decorator](adr/adr-grain-interfaces.md)
- [State Declaration via `@grain` decorator](adr/adr-state-declaration.md)
- [Turn-Based Concurrency Model](adr/adr-concurrency-model.md)

**Infrastructure**
- [JSON Serialization via `dataclasses` and `orjson`](adr/adr-serialization.md)
- [Dependency Injection via `injector`](adr/adr-dependency-injection.md)
- [Provider Interfaces as Hexagonal Ports](adr/adr-provider-interfaces.md)
- [Grain Directory — consistent hash ring](adr/adr-grain-directory.md)

**Topology and packaging**
- [Dev Mode — single-silo, in-process](adr/adr-dev-mode.md)
- [pyleans Is a Library, Not a CLI Tool](adr/adr-library-vs-cli.md)
- [One Package, Two Entry Points (`server` / `client`)](adr/adr-package-split.md)

**Cross-cutting**
- [Naming Convention](adr/adr-naming-convention.md)
- [Logging](adr/adr-logging.md)
- [Graceful Shutdown](adr/adr-graceful-shutdown.md)

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
pyproject.toml                     # single project file: pyleans metadata + tool config
src/
  pyleans/                         # pyleans framework source tree
    pyleans/                       # importable: import pyleans
      server/                      # silo runtime (heavy)
        string_cache_grain.py      # system grain: StringCacheGrain
        grains.py                  # system_grains() helper
      client/                      # lightweight client
      gateway/                     # TCP gateway protocol
      providers/                   # provider ABCs (ports)
    test/
  counter_app/                     # sample silo app (not pip-installed)
    counter_grain.py               # one file per grain
    main.py                        # Standalone silo
    __main__.py                    # python -m src.counter_app
    test/
  counter_client/                  # sample CLI client (not pip-installed)
    main.py                        # CLI entry point
    __main__.py                    # python -m src.counter_client
    test/
```

### Naming convention

`pyleans` is a pip-installable package (`pip install -e .` from the repo root; the
workspace `pyproject.toml` owns the package metadata and points hatchling at
`src/pyleans/pyleans/`). The sample apps
live under `src/` and are run as submodules — no pip install needed, just run
from the project root.

| Module | Type | Run with |
|---|---|---|
| `pyleans` | Framework (pip-installed) | Library, not runnable |
| `src.counter_app` | Sample app under `src/` | `python -m src.counter_app` |
| `src.counter_client` | Sample CLI under `src/` | `python -m src.counter_client` |

### One grain per file

Every grain class lives in its own file, named after the grain in snake_case:
`CounterGrain` → `counter_grain.py`, `StringCacheGrain` → `string_cache_grain.py`.

This applies to both framework-provided grains (in `src/pyleans/pyleans/server/`) and
application grains (in user packages like `src/counter_app/`). The grain's state
dataclass lives in the same file as the grain.

Test-only grains (defined inside test files) are exempt from this rule.

### Running the applications

All applications are run as Python modules. There are no installed console
scripts -- everything uses `python -m`.

```bash
# Install the framework in editable mode (from repo root)
pip install -e ".[dev]"

# Terminal 1: start the silo (blocks, Ctrl+C to stop)
python -m src.counter_app

# Terminal 2: use the CLI client
python -m src.counter_client get my-counter
python -m src.counter_client inc my-counter
python -m src.counter_client set my-counter 42
python -m src.counter_client get my-counter --gateway localhost:30000
```

The silo listens on gateway port 30000 (configurable). State persists to
`./data/storage/` and membership to `./data/membership.yaml` relative to the
working directory.

**Package manager**: `pip` with `venv` and editable installs (`pip install -e`).
`pyproject.toml` with `[project]` metadata, `[build-system]` using hatchling.
No `[project.scripts]` -- all entry points are `__main__.py` modules.

**Dependencies (pyleans)**:
- `injector` -- DI container (type-hint-based constructor injection for grains)
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
2. DI container with `injector` (GrainFactory, TimerRegistry, etc.)
3. Constructor injection for grains (framework + user services)
4. Grain activation / deactivation lifecycle (`on_activate`, `on_deactivate`)
5. `GrainRef` proxy with `__getattr__` forwarding
6. In-memory grain directory (dict, single-silo -- no hashing needed yet)
7. Turn-based scheduler (asyncio queue per grain)
8. `JsonFileStorageProvider`
9. Grain state via `self.state` (dataclass, loaded on activation, `orjson` serialization)
10. `YamlMembershipProvider` (file-based; no locking yet -- see Phase 2)
11. `MarkdownTableMembershipProvider` (human-readable membership table in Markdown for dev-mode inspection)
12. Idle collection (deactivate after timeout)
13. Grain timers
14. **Network port + asyncio adapter** (`INetwork`, `AsyncioNetwork`): the TCP-I/O abstraction consumed by every subsequent networking component. Mirrors `asyncio.start_server` / `asyncio.open_connection` one-to-one. See [adr-network-port-for-testability](adr/adr-network-port-for-testability.md).
15. **In-memory network simulator** (`InMemoryNetwork`): test-side adapter for the `INetwork` port — in-process registry, bounded-queue backpressure, failure-injection hooks, asyncio-parity server lifecycle. Used by every Phase 1 test that would otherwise bind a real port.
16. Counter example: standalone silo + CLI client via gateway protocol, built on `INetwork` from day one so the suite binds zero OS-level TCP ports.
17. **(Temporary) Retroactive network migration**: one-time task that transforms the current post-retrofit codebase into the state described by items 14–16. Structured as three commit-boundaried phases (create `net/` package → thread `INetwork` through production code → migrate tests). Archive after completion.

**Milestone**: `python -m src.counter_app` runs a standalone silo hosting a counter grain
that persists to a JSON file. `python -m src.counter_client` connects via ClusterClient
and the TCP gateway protocol. The test suite runs end-to-end without binding any OS-level TCP port.

### Phase 2: Multi-Silo Cluster

Add networking and distribution.

1. `SiloAddress`, `GrainId` identity types
2. TCP mesh transport (from [architecture/pyleans-transport.md](architecture/pyleans-transport.md) design)
3. File locking for file-based membership providers (YAML, Markdown-table) -- required once multiple silos read/write the shared membership file concurrently
4. Consistent hash ring with virtual nodes (30 per silo) — see [architecture/consistent-hash-ring.md](architecture/consistent-hash-ring.md)
5. Distributed grain directory partitioned across silos (eventually consistent)
6. Local directory cache with invalidation on membership change
7. Crash recovery: new partition owners query all silos to rebuild
8. Remote grain calls via transport
9. Silo startup/shutdown lifecycle
10. Placement strategies: random + prefer-local
11. **PostgreSQL membership provider** — production-grade backend. Per-silo row with an `etag_version` column updated via conditional `UPDATE ... WHERE etag_version = $expected` inside a transaction that also bumps a singleton version row. JSONB suspicions column for atomic append. Optional `LISTEN`/`NOTIFY` for snapshot broadcast. Required for any real multi-silo deployment; file-based providers are dev-only.
12. **PostgreSQL storage provider** — production-grade grain state backend. Single table keyed by `(grain_type, grain_key)` with a JSONB state column and monotonic per-row `etag` for optimistic concurrency. Shares the same PostgreSQL instance as the membership provider, so production deployments need only one stateful dependency.

**Milestone**: Two silo processes on localhost, a grain call from silo A executes on silo B. Cluster runs against PostgreSQL for both membership and grain storage (file-based providers retained for dev mode only).

### Phase 3: Streaming and Refinement

1. Full streaming provider interface (extends the Phase 1 basic `StreamProvider` ABC with `StreamId`, sequence tokens for resumable subscriptions, explicit pub/sub semantics, and per-subscription back-pressure — aligned with Orleans' `IStreamProvider`)
2. Stream subscriptions from grains (handle lifecycle, reactivation-safe resume)
3. Multi-silo streaming (fan-out over the transport from [task-26](tasks/task-26-tcp-cluster-transport.md))
4. **Redis Streams provider** — recommended external streaming backend. Redis is already a likely in-cluster dependency (membership and storage providers), has mature async Python bindings (`redis-py` asyncio client), provides durable append-only streams with consumer groups and replay, and is operationally light compared to Kafka. Pluggable behind the streaming provider interface — alternatives (Kafka, NATS JetStream) slot into the same port.

**Out of scope**: no embedded HTTP/WebSocket endpoint on the silo. External access is always through the gateway protocol, with web frameworks running as separate processes that use `pyleans.client.ClusterClient`. See [adr-cluster-access-boundary](adr/adr-cluster-access-boundary.md).

**Milestone**: Chat example with user grains and room grains exchanging messages via streams.

### Phase 4: Production Hardening (post-PoC)

- Redis/etcd membership provider
- Redis/S3 storage provider
- MQTT gateway transport
- WebSocket gateway transport (browser-facing complement to MQTT)
- Reentrancy support
- Reminders
- Grain call filters (interceptors for cross-cutting concerns: auth, logging, tracing, rate limiting)
- Stateless workers (parallel activations for stateless grains — throughput optimization)
- Metrics and observability
- Multi-channel resource-based placement strategy (e.g. ML-hardware availability: route grains that need a GPU/TPU/NPU to silos that advertise the matching resource channel)
- Dashboard / admin UI (external app consuming `SiloManagement` + membership)
- Documentation and packaging for PyPI

---

