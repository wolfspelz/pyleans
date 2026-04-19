---
status: accepted
date: 2026-04-18
tags: [cluster, directory, transport, virtual-actors, hexagonal]
---

# Single Activation Is a Cluster Responsibility, Enforced by Directory and Transport

## Context and Problem Statement

The virtual-actor model's defining invariant is: **at any moment, at most one activation of a given `GrainId` exists across the cluster.** The grain's identity (`grain_type` + `key`) is the serialisation point for everything that happens to it — state, calls, timers, streams.

A multi-silo deployment must preserve this invariant. But the physical architecture allows each silo to run the same Python process, load the same storage provider, and happily activate `CounterGrain("a")` independently — which gives two activations, each with its own state, racing on the shared storage backend. That is exactly the failure mode the virtual-actor model is designed to prevent.

Where should enforcement live? We need a clear answer before Phase 2 begins — the answer dictates the shape of every subsystem (directory, transport, lifecycle, placement, failure detection, gateway protocol) that follows.

The secondary question is whether external clients need to know which silo owns which grain. In Orleans they do not — clients connect to any gateway and the cluster routes internally. We must decide whether pyleans keeps that property.

## Considered Options

- **A — Cluster-enforced single activation via distributed grain directory on a consistent hash ring.** Directory + cluster transport live inside the cluster; the runtime consults the directory and forwards remote calls. Clients stay thin.
- **B — Per-silo activation with shared storage as the serialisation point.** Each silo activates locally; storage-layer etag CAS "wins" races.
- **C — Side-channel coordination file (shared JSON directory on disk).** A shared file lists `grain_id → owner silo`; silos read/write it with optimistic CAS.
- **D — Client-side routing.** The client receives the membership table, runs the hash ring locally, dials the owning silo's gateway directly.
- **E — Central authoritative directory on a single "leader" silo.** One designated silo owns the directory; all others proxy to it.

## Decision

**Option A.** Single activation is a **cluster responsibility**, enforced at the intersection of three first-class subsystems:

1. **Membership** — every silo sees the same set of active peers (per [adr-provider-interfaces](adr-provider-interfaces.md)).
2. **`IGrainDirectory`** — partitioned by a consistent hash ring over `hash_grain_id(grain_id)`, eventually consistent (per [adr-grain-directory](adr-grain-directory.md)). `resolve_or_activate()` is the single method that binds a grain id to a silo.
3. **`IClusterTransport`** — the silo-to-silo mesh (per [adr-cluster-transport](adr-cluster-transport.md)) that carries directory RPCs, heartbeats, and remote grain invocations.

The runtime consults the directory inside `GrainRuntime.invoke()`; if the grain is remote, it forwards over `IClusterTransport`. External clients connect to **any** gateway port ([adr-cluster-access-boundary](adr-cluster-access-boundary.md)) and see the identical protocol regardless of where the grain lives — silo transparency is a consequence of the architecture, not a separate feature.

The invariant applies to the compound identity: `(grain_type, key)`, i.e. a `GrainId` including its class. Two grain classes can have the same key and they are independent activations; two silos cannot host the same `GrainId` simultaneously.

## Consequences

- **Phase 2 must ship the full cluster stack**: hash ring, distributed directory, cluster transport, connection management, failure detector, directory cache, directory recovery, lifecycle stages. None of these can be substituted by a simpler mechanism without violating the contract. Task dependencies and scope are in [docs/tasks/README.md §Phase 2](../tasks/README.md#phase-2-tasks).
- **External clients remain thin.** `ClusterClient` knows only about gateway addresses; it does not know about the hash ring, directory, or silo identity. Client-facing APIs do not change when cluster topology does.
- **Grain authors get the guarantee they expect.** A grain method implementation can assume single-threaded, single-activation semantics without qualification.
- **Eventually-consistent semantics are accepted**: briefly during membership changes, two silos may hold directory entries that disagree. See [adr-grain-directory](adr-grain-directory.md) for the consistency model and its rationale. Strong consistency (Orleans 9.0+ style) is deferred to post-PoC.
- **Operational cost is real.** The cluster stack is substantial — roughly 21 tasks and ~4000 lines of production code. That cost is accepted as the price of delivering the virtual-actor contract honestly. Shortcut implementations were evaluated (Options B, C, E) and rejected.
- **Gateway code stays small.** The gateway listener dispatches to `runtime.invoke()` without knowing or caring about cluster routing. Silo transparency is a structural consequence of placing the routing hook inside the runtime, not inside the gateway.

## Pros and Cons of the Options

### Option A — Cluster-enforced single activation via distributed grain directory on a consistent hash ring (chosen)

Every silo participates in a shared `IGrainDirectory` partitioned by a consistent hash ring over `hash_grain_id(grain_id)`. The directory answers one question: *given a grain id, which silo owns its activation?* The answer is computed by `resolve_or_activate()`, which on first call registers an owner silo (chosen by a `PlacementStrategy`) and on subsequent calls returns the existing owner.

The runtime consults the directory inside `GrainRuntime.invoke()`. If the grain is owned by the local silo, the call proceeds as in Phase 1. If owned by a remote silo, the runtime forwards the call over `IClusterTransport` and returns the result to the caller.

Pros:

- Enforces the single-activation invariant directly at the level where it matters: the runtime, not the storage layer.
- Silo transparency is a **structural consequence** — the gateway listener's `_dispatch` already hands requests to `runtime.invoke()`, so the routing logic is picked up for free. A client sees the identical protocol through any gateway.
- Consistent with every other cross-cutting concern in the repo (hexagonal ports for storage, membership, streaming, network — see [adr-provider-interfaces](adr-provider-interfaces.md)).
- Scales: the hash ring distributes entries across silos, no single node is a directory bottleneck.
- Reuses established Orleans design that has proven operationally viable over a decade.

Cons:

- Substantial implementation cost: 21 Phase 2 tasks, ~4000 LOC of production code plus tests.
- Eventually-consistent model accepts brief directory inconsistency during membership changes (documented in [adr-grain-directory](adr-grain-directory.md)). Strong consistency would cost a consensus protocol, deferred post-PoC.
- Requires disciplined lifecycle management so calls are not accepted before the cluster is joined and the directory primed.

### Option B — Per-silo activation with shared storage as the serialisation point (rejected)

Each silo activates grains locally, unaware of peers; the only serialisation is the etag CAS inside the storage provider. The storage layer "wins" races.

Pros:

- Trivial to implement — no cluster stack needed.
- Each silo is independent; membership changes are easy.

Cons:

- **Violates the single-activation invariant directly.** Two activations exist simultaneously while racing on writes. Behaviour between writes is divergent and observable.
- In-memory state on each silo drifts from the other — timers fire twice, streams produce duplicates, grain instances hold inconsistent caches, `on_activate` / `on_deactivate` hooks run twice per logical grain.
- Storage-only serialisation is a write-throughput bottleneck by design and a lie about semantics by accident. The grain author is told they have a single-threaded actor; they do not.
- Debugging is nearly impossible: behaviour depends on which silo received which call and how the storage backend ordered the CAS attempts.

This is the "two silos with split storage" pattern a naïve implementer might reach for. It is not acceptable at any phase.

### Option C — Side-channel coordination file (shared JSON directory on disk) (rejected)

Replace the distributed directory with a shared file (`directory.json`) on a network filesystem or local disk. Silos read/write the file with optimistic CAS to register grains.

Pros:

- Smaller code footprint than a full distributed directory.
- Works in a single-host demo without a cluster transport.

Cons:

- **A worse, hidden re-implementation of the directory.** All the same concurrency concerns (ownership, cache invalidation, atomic updates, partial failure) appear — but now solved by ad-hoc file locking instead of a first-class subsystem.
- Assumes a shared filesystem, limiting deployment topologies.
- Hides the real cluster design behind a substitute — contributors reading the code learn nothing about hash rings, directories, or cluster transport. The architecture would have to be re-learned whenever a production feature needed it (failure recovery, cache tuning, directory migration).
- Does not solve the routing problem. Once you know the owner, you still need a transport to reach it. Adding the transport is most of the work anyway.

### Option D — Client-side routing (rejected)

The client receives the membership table, runs the same hash-ring logic, and dials the owning silo's gateway directly.

Pros:

- Silos stay simpler — no inter-silo routing code.
- Direct path from client to grain owner, no extra forwarding hop.

Cons:

- **Breaks silo transparency.** Every client has to know and maintain the cluster topology, rebuild the hash ring on membership changes, and handle routing failures. Thin clients (curl, a browser SPA behind a WebSocket adapter, an MQTT-over-IoT device) cannot reasonably carry that logic.
- The cluster's `IClusterTransport` would still need to exist for silo-to-silo messaging (heartbeats, directory RPCs, failure detection). So this option does not *replace* the cluster transport; it *duplicates* routing logic in the client.
- Security posture suffers: every client becomes a participant in cluster-internal protocols, must be trusted with membership data, and must be upgraded in lockstep with the cluster.

### Option E — Central authoritative directory on a single "leader" silo (rejected)

One designated silo owns the directory; all others proxy to it.

Pros:

- Simple consistency model — one writer.
- No hash-ring machinery needed.

Cons:

- Single point of failure for the directory.
- Throughput bottleneck on the leader as cluster grows.
- Leader election is not in scope ([adr-grain-directory](adr-grain-directory.md) explicitly defers strong consistency and consensus machinery to post-PoC).
- The hash-ring approach already provides the benefits without the concentration risk.

## Related

- [adr-grain-directory](adr-grain-directory.md) — consistency model and hash-ring decision.
- [adr-cluster-transport](adr-cluster-transport.md) — the pluggable silo-mesh transport.
- [adr-cluster-access-boundary](adr-cluster-access-boundary.md) — gateway is the sole external entry point.
- [adr-provider-interfaces](adr-provider-interfaces.md) — hexagonal ports, including membership.
- [adr-network-port-for-testability](adr-network-port-for-testability.md) — `INetwork` port the cluster transport builds on.
- [pyleans-transport.md](../architecture/pyleans-transport.md) — long-form protocol and framing spec.
