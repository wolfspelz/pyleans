---
status: accepted
date: 2026-04-18
tags: [network, transport, testing, hexagonal]
---

# Pluggable Network Port — Abstract TCP for In-Memory Test Simulation

## Context and Problem Statement

pyleans is a fundamentally network-bound system. Phase 1 already ships a TCP gateway protocol for external clients ([adr-cluster-access-boundary](adr-cluster-access-boundary.md)). Phase 2 adds a full-mesh silo-to-silo TCP transport ([adr-cluster-transport](adr-cluster-transport.md)). Phase 3 will stream events across that mesh. Phase 4 adds MQTT and WebSocket gateway framings. **Every non-trivial failure mode pyleans exhibits in production will originate in the network layer** — connection loss, partial sends, backpressure exhaustion, timeouts, reconnect storms, handshake mismatches, message reordering. That is exactly what we must be able to test deterministically and thoroughly.

Using the real OS-level TCP/IP stack in unit and integration tests is ill-suited to this goal:

- **Real sockets consume kernel resources.** Every listener holds a file descriptor; every failed-and-unclean test leaks one until process exit. Phase 2's integration harness plans ~60 listeners per run across 13 scenarios ([task-02-18-multi-silo-integration-tests.md](../tasks/task-02-18-multi-silo-integration-tests.md)). At scale this exhausts per-process FD limits (default 1024 on Linux).
- **Kernel scheduling introduces non-determinism.** Network-layer unit tests need to assert on ordering, backpressure thresholds, and failure-signal timing. With the real stack in the loop, the same test passes on one machine and flakes on another.
- **Failure modes are hard to reproduce.** `ConnectionResetError`, mid-frame peer close, write stalls past the TCP window, asymmetric half-open connections — these are routine in production and trivial to simulate in-memory, but wrestling a real kernel into producing them on demand is fragile (requires `iptables`, `tc netem`, or test-specific network namespaces).
- **Even `port=0` doesn't make tests kernel-free.** Ephemeral port assignment avoids explicit collisions but still binds real FDs, still accumulates TIME_WAIT, and still routes through the full TCP stack. The repository has already observed intermittent "port in use" symptoms originating in this layer.
- **Test parallelism is constrained** by the shared-kernel nature of real networking. `pytest-xdist` workers share the host's ephemeral port range.

Production and development, by contrast, **must use the real TCP stack** — pyleans exists to run across network boundaries. The production path is not negotiable. What is negotiable is whether the runtime gets TCP directly from `asyncio.start_server` / `asyncio.open_connection`, or from a port that admits a second implementation.

## Considered Options

### Option A — Abstract the network behind an `INetwork` port; inject at test time (chosen)

A thin port mirrors asyncio's native `start_server` / `open_connection` surface. Two implementations:

- **`AsyncioNetwork`** — pass-through to `asyncio.start_server` / `asyncio.open_connection`. Production and dev default. Zero behavior change vs direct asyncio usage.
- **`InMemoryNetwork`** — per-instance in-process registry, hand-wired `StreamReader` / `StreamWriter` pairs, bounded-queue backpressure simulation, explicit failure-injection hooks. For tests only.

### Option B — Keep using `asyncio.start_server` directly, use `port=0` and clean teardown (rejected)

What pyleans does today. Avoids immediate collisions but does not address the deeper problems: kernel involvement, FD consumption, non-deterministic failure-mode coverage, parallelism limits. Adequate for Phase 1's small test set, insufficient for Phase 2's integration harness.

### Option C — Use `socket.socketpair()` for test connections (rejected)

A POSIX pair of connected AF_UNIX sockets. Avoids port binding but still uses real kernel FDs and still routes data through the kernel. Does not scale (FD limits) and fails the "no real OS-level networking in tests" bar.

### Option D — Adopt a third-party library (rejected)

Surveyed: `anyio` memory streams (object-based, would require an adapter layer and adds a production dep), `trio.testing.memory_stream_pair` (trio-only), `pytest-asyncio-network-simulator` (abandoned, monkeypatches asyncio internals). No mainstream asyncio library provides a turnkey in-memory network simulator. Large async Python codebases (aiohttp, grpclib, asyncpg) hand-roll small adapters or accept real sockets at the integration tier. A ~300-LOC in-house adapter is the proven pattern.

## Decision

**Option A.** All TCP communication in pyleans flows through an `INetwork` port.

- **Production and dev** run against `AsyncioNetwork`, which is a thin pass-through to the real TCP stack. There is no semantic or performance change vs calling `asyncio` directly.
- **Unit and integration tests** run against `InMemoryNetwork`, which simulates TCP connections end-to-end in-process — no ports bound, no file descriptors consumed, no kernel involvement.
- The simulator is **for testing only**. It is not a deployment target, not a performance benchmark rig, and not a chaos-engineering tool.

### Scope the simulator must cover

For the simulator to be a faithful testing substrate it must reproduce every behavior pyleans' runtime depends on:

- **Unit tests**: connection open/close, `StreamReader` / `StreamWriter` semantics including `drain()` backpressure above a high-water mark, EOF propagation, correct stdlib exception types (`ConnectionRefusedError` on unbound address, `ConnectionResetError` on peer reset, `IncompleteReadError` on mid-frame close). Goal: zero OS-level ports are bound during the entire unit-test run.
- **Integration tests** where multiple pyleans components communicate through the simulator (e.g. remote grain calls across simulated silos in Phase 2, gateway round-trips in Phase 1): simulated multi-peer interconnect with the features remote communication actually depends on — ordered delivery per connection, independent per-connection flow control, failure injection for reset / peer-close / timeout, server lifecycle matching `asyncio.Server` semantics (stops accepting on `close()`, `wait_closed()` awaits in-flight handlers without cancelling them). Goal: the entire cluster-level behavior — remote calls, directory lookups, failure detection — can be exercised end-to-end without the OS network stack.

### Scope the simulator does NOT cover

- Packet-level chaos (latency injection, loss, reorder, jitter). If needed for specific tests, model it at the application layer above the port.
- TLS. The simulator accepts an `ssl` parameter for API parity and ignores it with a DEBUG log. TLS behaviour is the responsibility of the asyncio adapter and the deployment.
- Multi-event-loop / multi-process simulation. Everything runs on the pytest event loop.
- Performance characterisation. Throughput and latency numbers from the simulator are not indicative of production behaviour.

### Protocols in scope vs out of scope

The "no OS ports in tests" rule applies **only to the home-grown protocols pyleans ships and owns end-to-end**:

- the TCP silo-to-silo interconnect ([adr-cluster-transport](adr-cluster-transport.md)), and
- the gateway protocol ([adr-cluster-access-boundary](adr-cluster-access-boundary.md)).

These run over `INetwork` and MUST be testable in-process against `InMemoryNetwork`. Every new failure mode in these protocols is a pyleans concern and deserves deterministic coverage without kernel involvement.

Third-party protocols that might be plugged in behind `IClusterTransport` / `IGatewayTransport` — MQTT brokers, Zenoh routers, HTTP/2 gRPC meshes, anything else — are **out of scope for this rule**. Their client libraries use their own sockets, and reimplementing those libraries against an in-memory simulator is both impractical and pointless: the value a third-party transport brings is precisely its battle-tested wire implementation. Tests for those adapters use whatever the library's own testing story recommends (real broker in a container, library-provided fakes, recorded fixtures). Pyleans' contribution is only the adapter code that maps the external protocol onto our port ABCs; that adapter itself can still be tested with doubles where useful. Conversely, nothing in this ADR requires a third-party adapter to ship an in-memory simulator; a real broker at integration-test time is acceptable.

## Consequences

- **Application and runtime code must not call `asyncio.start_server` or `asyncio.open_connection` directly.** All TCP I/O goes through the `INetwork` port. Runtime and test linting enforce this.
- **Tests gain deterministic access to failure modes** that are impractical to produce with real sockets: reset mid-write, asymmetric peer close, backpressure stalls at known thresholds. Failure-path coverage rises substantially with no flakiness cost.
- **Zero OS-level ports are bound during the unit-test run**, and integration-test runs use only as many ports as tests that opt explicitly into the asyncio adapter (approximately none — smoke tests that verify the adapter itself).
- **~300 LOC of in-memory adapter code** to own and maintain. Small, well-bounded, parallel to `AsyncioNetwork` so drift is visible.
- **Phase 2 and beyond inherit the abstraction for free.** `TransportOptions` accepts an `INetwork`, so the TCP mesh transport, gateway listener, and future MQTT/WebSocket adapters all share the same injection seam.
- **Dev smoke tests still run against real TCP** by constructing `AsyncioNetwork` explicitly. This keeps us honest about the production path without leaning on it for routine testing.
- **Gap the simulator can't close**: real-kernel bugs (rare but real) and genuinely cross-host scenarios are not in scope. Those remain the responsibility of manual validation and post-PoC end-to-end testing.

## Related

- [adr-cluster-transport](adr-cluster-transport.md) — pluggable transport layer (`IClusterTransport`, `IGatewayTransport`). Sits one layer above `INetwork` in Phase 2: transports use the network port for their TCP I/O.
- [adr-cluster-access-boundary](adr-cluster-access-boundary.md) — gateway protocol is the sole external entry point. The gateway listener uses `INetwork` for its TCP accept loop.
- [adr-provider-interfaces](adr-provider-interfaces.md) — hexagonal ports as the cross-cutting pyleans pattern. `INetwork` is another port in that family.
- [pyleans-transport.md](../architecture/pyleans-transport.md) — long-form transport design; §4 assumes an underlying reliable stream abstraction that `INetwork` supplies.
