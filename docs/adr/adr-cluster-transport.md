---
status: accepted
date: 2026-04-18
tags: [transport, cluster, gateway, hexagonal]
---

# Pluggable Transport — Start with TCP, Add Others Later

## Context and Problem Statement

pyleans silos must communicate intra-cluster (silo ↔ silo) and accept gateway connections from external clients (web servers, CLIs, IoT devices). Many candidate protocols exist (custom TCP, MQTT, WebSocket, Zenoh, ZeroMQ, Redis pub/sub, AMQP, Unix sockets), each with different trade-offs on latency, ordering guarantees, deployment complexity, and ecosystem fit.

The question is not only *which* protocol to start with, but whether the transport layer should be **pluggable** at all.

## Considered Options

### Option A — Pluggable transport behind an ABC (chosen)

Define `IClusterTransport` (silo mesh) and `IGatewayTransport` (external clients) ports. Ship a custom asyncio TCP mesh as the default adapter. Add MQTT (IoT), WebSocket (browsers), Zenoh (edge) as additional adapters over time without touching core runtime.

### Option B — Fixed transport, Orleans-style (rejected)

Orleans hardcodes a custom TCP-based silo-to-silo protocol with its own framing, handshake, and clustering layer. There is no transport abstraction — runtime and wire format are co-designed.

Benefits of this approach:

- **One code path**, one set of failure modes to test and operate.
- **Tight integration** of framing, backpressure, and directory updates; co-evolution of protocol and runtime is cheap.
- **Known, tuned performance** characteristics; no per-adapter surprises.
- **Simpler mental model** for new contributors — no port/adapter layering for a concern that is usually implementation-detail-stable.
- **Smaller API surface** to stabilize — no risk that the ABC leaks yesterday's assumptions.

Rejected for pyleans because:

- We explicitly want to support non-TCP environments: **MQTT** for IoT, **WebSocket** for browsers, **Zenoh** for edge. Orleans' TCP assumption is acceptable for datacenter deployments but not for our target mix.
- Hexagonal architecture is already the repo-wide pattern (storage, membership, streaming are all pluggable ports — see [adr-provider-interfaces](adr-provider-interfaces.md)). Making transport the one fixed subsystem would be an inconsistent exception.

Full protocol comparison and design details are in [pyleans-transport.md](../architecture/pyleans-transport.md).

## Decision

Option A. The transport layer is pluggable behind `IClusterTransport` and `IGatewayTransport`.

- Phase 2 ships a custom asyncio TCP mesh as the default intra-cluster transport.
- MQTT, WebSocket, and Zenoh adapters are additive and deferred to post-PoC phases.
- The ABC is the only thing the core runtime depends on; adapters depend on the ABC, not the reverse.

## Consequences

- Core runtime depends only on the transport ABC — swapping or adding backends is additive, no core changes.
- We pay an abstraction tax: every adapter must implement the ABC, and the ABC must be designed carefully not to leak TCP-specific assumptions. This is a real risk given TCP is the first and, for now, only implementation.
- The first implementation effort focuses on custom TCP; MQTT/WS/Zenoh adapters are post-PoC.
- [pyleans-transport.md](../architecture/pyleans-transport.md) is the single source of truth for the protocol comparison, framing, handshake, backpressure design, and interface specification.

## Related

- [adr-single-activation-cluster](adr-single-activation-cluster.md) — `IClusterTransport` is one of the three subsystems (with membership and `IGrainDirectory`) that jointly enforce single activation; this ADR defines the port that carries directory RPCs and remote grain invocations.
- [pyleans-transport.md](../architecture/pyleans-transport.md) — full design and protocol comparison
- [adr-provider-interfaces](adr-provider-interfaces.md) — the other pluggable ports (storage, membership, streaming)
- [adr-dev-mode](adr-dev-mode.md) — Phase 1 ships without any transport
