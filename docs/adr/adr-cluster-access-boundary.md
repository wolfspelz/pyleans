---
status: accepted
date: 2026-04-18
tags: [transport, gateway, hexagonal, web]
---

# Cluster Access Boundary — Gateway Protocol Is the Sole External Entry Point

## Context and Problem Statement

External consumers of a pyleans cluster come in many shapes: CLIs, mobile apps, browser SPAs, IoT devices, internal services, FastAPI apps serving a web UI, etc. A recurring design question is whether the silo should host additional listener surfaces — e.g. an embedded HTTP/JSON endpoint (`POST /grains/{type}/{key}/{method}`), an OpenAPI-generated REST layer, a GraphQL server — to cover these use cases directly from the silo process.

The alternative is a clean boundary: silos accept exactly one external protocol (the gateway protocol shipped in Phase 1 via `pyleans.gateway`), and any other access model (HTTP, WebSocket, GraphQL, MQTT) is the responsibility of a **separate process** that uses `pyleans.client.ClusterClient` internally to talk to the cluster.

## Considered Options

### Option A — Gateway protocol is the sole silo-facing external surface (chosen)

Silos expose two network surfaces:

- **Silo-to-silo TCP mesh** ([adr-cluster-transport](adr-cluster-transport.md), Phase 2) — internal mesh traffic, never reached by external callers.
- **Gateway protocol** for external clients — Phase 1 ships TCP framing; WebSocket and MQTT are additional framings of the *same* gateway protocol, added in Phase 4 as `IGatewayTransport` adapters. The wire format of the gateway protocol varies; the semantics do not.

HTTP APIs, browser apps, and any other application protocol run as separate processes that import `pyleans.client` and use `ClusterClient` to call grains. The silo knows nothing about HTTP routing, CORS, web sessions, auth tokens, OpenAPI, or rate limiting.

### Option B — Embedded HTTP/REST endpoint on the silo (rejected)

Silo runs an HTTP listener alongside the gateway, auto-exposing grain methods as REST endpoints.

Benefits:
- Zero extra process for simple "call grain from curl" use cases.
- Easier demo surface for beginners.

Rejected because:

- **Couples the silo runtime to web-framework concerns** — CORS, auth schemes, request validation, OpenAPI generation, rate limiting, session cookies, TLS cert rotation, WAF integration — all of which evolve independently of grain semantics and on a different cadence.
- **Python already has excellent web frameworks** (FastAPI, Starlette, Flask). Duplicating even a sliver of them inside the silo violates the "pyleans is a library, not a framework" stance from [adr-library-vs-cli](adr-library-vs-cli.md).
- **Grain method signatures are Python-native** — complex types, exceptions, kwargs — and do not map cleanly or safely to HTTP semantics. A faithful auto-exposure requires either lossy conversions or a schema-definition pass that is effectively the same work as writing a thin FastAPI layer on the client side.
- **Operational coupling** — the web tier needs its own deployment cadence (patching for CVEs in the framework, adjusting routes, changing auth). Baking a web listener into the silo means every silo carries that weight whether it is consumed by a browser or not, and cluster restarts are needed for routes that have nothing to do with grain code.

### Option C — Embedded WebSocket gateway as a separate concept (rejected; subsumed by Option A)

One could imagine a "WebSocket gateway" that is distinct from the gateway protocol — something that serves browser-native JSON events over WebSocket independently of the Phase 1 framing.

Rejected because the `IGatewayTransport` port from [adr-cluster-transport](adr-cluster-transport.md) already covers this: a WebSocket adapter frames the existing gateway protocol in WebSocket binary frames. That is additive, not a second concept. No separate "web server gateway" subsystem is needed.

## Decision

**Option A.** The gateway protocol is the sole external entry point to a silo. All HTTP/WebSocket/REST/GraphQL surfaces are built as separate processes (or separate in-process components of the user's application) that use `pyleans.client.ClusterClient` to talk to the cluster.

Concretely:

- **No Phase ever introduces an embedded web server on the silo.** This is a durable commitment, not a "defer for now."
- **Sample apps demonstrating HTTP access** (e.g. FastAPI + pyleans) live under `examples/` as their own programs, not as features of the silo.
- **The `IGatewayTransport` port** can host alternative wire formats (WebSocket, MQTT) for the gateway protocol itself. That is still consistent with this ADR: the *protocol* is unchanged, only its *framing* varies.
- **Co-hosting a web framework in the silo process** remains possible via `Silo.start_background()` — a single Python process can run both a silo and a FastAPI app, using an in-process `ClusterClient` for zero-network-hop grain calls. That is explicitly still Option A: the web framework is a **caller** of the cluster, not a feature of the silo runtime.

## Consequences

- The silo's public API surface stays narrow and stable: one protocol (gateway) with pluggable framings via `IGatewayTransport`.
- Users who want HTTP endpoints write a thin adapter process using `ClusterClient`. In practice this is ~20–50 lines of FastAPI + pyleans.
- Operationally, the web tier scales independently of the silo tier. A cluster hosting stateful grains is not restarted when HTTP routes change.
- No OpenAPI/REST autogeneration story in pyleans itself. Teams that want one build it on top of `ClusterClient`, with full control over schema, auth, and validation.
- Documentation and demos must be explicit about the boundary so newcomers don't look for "how do I expose an HTTP endpoint from my grain" and conclude pyleans is missing something. The answer is "you write a web-server process that uses the client" — with example code.

## Related

- [adr-single-activation-cluster](adr-single-activation-cluster.md) — silo transparency (client hits *any* gateway, cluster routes to owner) is a structural consequence of the single-activation contract. This ADR pins the external boundary where that transparency is delivered.
- [adr-cluster-transport](adr-cluster-transport.md) — pluggable transport ports. This ADR constrains what `IGatewayTransport` exposes semantically; it does not constrain framing.
- [adr-library-vs-cli](adr-library-vs-cli.md) — pyleans is a library.
- [adr-package-split](adr-package-split.md) — `pyleans.server` (silo) vs `pyleans.client` (lightweight cluster client).
- [pyleans-transport.md](../architecture/pyleans-transport.md) §7 — recommends WebSocket and MQTT as gateway transport *framings* (consistent with this ADR).
