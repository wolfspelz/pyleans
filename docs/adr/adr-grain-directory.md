---
status: accepted
date: 2026-04-18
tags: [directory, cluster, placement]
---

# Grain Directory — Consistent Hash Ring, Eventually Consistent

## Context and Problem Statement

In a multi-silo cluster, every silo must agree on which silo currently hosts a given `GrainId`. Orleans offers two models:

- **pre-9.0**: eventually consistent — a consistent hash ring with virtual nodes, no consensus protocol. Brief inconsistencies possible during membership changes.
- **9.0+**: strongly consistent — requires Raft-like consensus machinery.

The second is significantly more complex. We need a placement model that scales across silos but keeps Phase 1 trivial.

## Decision

- **Phase 1 (single silo)**: in-memory directory (simple `dict`). No hash ring, no partitions — one silo owns everything.
- **Phase 2 (multi-silo)**: consistent hash ring with virtual nodes (30 per silo), eventually consistent (pre-Orleans 9.0 style). Each silo owns a partition of the hash ring and maintains a local directory cache.
- Strong consistency (Orleans 9.0+ style) is deferred to post-PoC.

## Consequences

- Phase 1 code is dramatically simpler — no hashing, no partition ownership, no cache invalidation.
- **The directory is the enforcement point for the single-activation contract** ([adr-single-activation-cluster](adr-single-activation-cluster.md)): `resolve_or_activate(grain_id)` is the single method that binds a `GrainId` to exactly one owner silo across the cluster. Without the directory, the contract cannot be honoured; substituting a different mechanism (per-silo activation with etag-CAS, side-channel files, etc.) was evaluated and rejected there.
- During membership changes in Phase 2, briefly inconsistent views are possible (same grain activated on two silos). Accepted for PoC; documented as a known limitation. Directory recovery (Phase 2 task 02-15) converges the cluster back to exactly one activation after the transient.
- Moving to strong consistency later is a significant change but does not affect grain-author APIs.

## Related

- [adr-single-activation-cluster](adr-single-activation-cluster.md) — the single-activation contract this directory enforces.
- [adr-dev-mode](adr-dev-mode.md)
- [adr-provider-interfaces](adr-provider-interfaces.md) — membership provider is the input to the directory.
