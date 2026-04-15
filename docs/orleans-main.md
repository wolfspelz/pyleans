# Orleans Virtual Actor Framework -- Documentation for Pyleans

This documentation captures the key features, concepts, and implementation details of
Microsoft Orleans, a virtual actor framework for .NET. Its purpose is to serve as the
reference for building **pyleans** -- a Python implementation of the Orleans virtual actor model.

## What is Orleans?

Orleans is a framework for building distributed, scalable, high-availability applications
using the **virtual actor model** (called "grains" in Orleans). It was created at Microsoft
Research and has been used in production for Xbox/Halo, Skype, Azure, and many other services.

The core idea: developers write business logic as classes with state (grains), and the
framework handles distribution across a cluster of nodes, persistence, activation/deactivation,
concurrency, networking, and streaming -- transparently.

## Module Documentation

### [Grains (Virtual Actors)](orleans-grains.md)
The fundamental building block. Covers grain identity (type + key), interfaces, lifecycle
(activation, deactivation, idle collection), single-threaded turn-based concurrency,
reentrancy, stateless workers, timers/reminders, call filters, and placement strategies.

### [Cluster and Membership](orleans-cluster.md)
The runtime infrastructure. Covers silo architecture, cluster formation, the membership
protocol (failure detection, suspicion voting, Lifeguard), membership table and providers,
consistent hashing ring, the distributed grain directory, silo lifecycle, and client
connections.

### [Persistence and Storage](orleans-persistence.md)
How grain state survives restarts. Covers the declarative persistence model
(`IPersistentState<T>`), the `IGrainStorage` interface, ETags and optimistic concurrency,
built-in storage providers (Azure, DynamoDB, SQL, Redis, in-memory), custom providers,
serialization, state versioning, and event sourcing with journaled grains.

### [Streaming](orleans-streaming.md)
Pub/sub integrated with the actor model. Covers stream identity, producers/consumers,
implicit vs explicit subscriptions, stream providers (Event Hubs, SQS, in-memory),
delivery guarantees, rewindable streams, filtering, the PubSub system, pulling agents,
queue caching/backpressure, and custom provider implementation.

### [Networking, Serialization, and RPC](orleans-networking.md)
The wire protocol. Covers the `[GenerateSerializer]` system, the binary wire format,
version tolerance, immutability optimizations, the full grain call flow (proxy -> directory
-> dispatch -> response), message structure, silo-to-silo TCP connections, client gateway
protocol, connection multiplexing, one-way messages, cancellation propagation, and generic
grain support.

### [Advanced Features](orleans-advanced.md)
Cross-cutting concerns. Covers turn-based concurrency in depth, all reentrancy mechanisms,
ACID transactions across grains, grain observers (callbacks), grain services (silo singletons),
request context propagation, grain extensions, dependency injection, cancellation/timeouts,
messaging guarantees, all placement strategies, and stateless workers.

## Key Concepts for Pyleans

These are the essential abstractions to implement, in priority order:

### Tier 1 -- Core (minimum viable framework)
1. **Grain** -- A class with identity (type + key), in-memory state, and async methods
2. **Silo** -- A process that hosts grain activations, runs an asyncio event loop
3. **Grain Reference** -- A proxy object that routes method calls to the correct silo
4. **Grain Directory** -- Maps grain identity to the silo currently hosting it
5. **Membership** -- Tracks which silos are alive in the cluster
6. **Storage Provider** -- Persists grain state (start with file-based JSON)
7. **Single-activation guarantee** -- Only one activation per grain identity cluster-wide
8. **Turn-based concurrency** -- One message processed at a time per grain (asyncio natural fit)

### Tier 2 -- Essential features
9. **Placement Strategies** -- Where to create new activations (random, prefer-local, hash-based)
10. **Timers** -- In-grain periodic callbacks
11. **Reminders** -- Persistent, durable timers that survive grain deactivation
12. **Idle Collection** -- Deactivate grains after inactivity timeout
13. **Grain Lifecycle Hooks** -- on_activate, on_deactivate

### Tier 3 -- Power features
14. **Streaming** -- Pub/sub with stream providers
15. **Reentrancy** -- Allow interleaved processing for marked grains/methods
16. **Grain Observers** -- Push notifications to external subscribers
17. **Transactions** -- ACID across multiple grains
18. **Stateless Workers** -- Multiple activations for compute-heavy grains

## Architecture Mapping: Orleans (.NET) -> Pyleans (Python)

| Orleans Concept | Python Equivalent |
|---|---|
| Silo (process) | asyncio-based Python process |
| Grain class | Python class with decorator `@grain` |
| Grain interface (C# interface) | Python ABC or Protocol |
| `[GenerateSerializer]` | `dataclasses` + JSON/msgpack |
| `Task<T>` / `ValueTask<T>` | `asyncio` coroutines |
| `await grain.Method()` | `await grain_ref.method()` |
| Dependency Injection | Constructor injection or simple DI container |
| `IGrainStorage` | Abstract base class with read/write/clear |
| `IMembershipTable` | Abstract base class (file, Redis, etcd, etc.) |
| `IStreamProvider` | Abstract base class (in-memory, Redis pub/sub, etc.) |
| ASP.NET co-hosting | FastAPI/ASGI co-hosting |

## Design Principles to Preserve

1. **Location transparency** -- Calling a grain looks the same whether it's local or remote
2. **Virtual actors** -- Grains always exist conceptually; activated on demand, deactivated when idle
3. **Single-threaded turns** -- No locks needed inside grain code
4. **Provider model** -- Storage, membership, and streaming are pluggable
5. **Hexagonal architecture** -- Business logic (grains) at the center, infrastructure (providers) at the edges
