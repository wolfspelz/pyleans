---
status: accepted
date: 2026-04-18
tags: [topology, single-silo, phase-1]
---

# Dev Mode — Single-Silo, In-Process

## Context and Problem Statement

Orleans offers `UseLocalhostClustering()` — a single-process mode for local development and tests, with no networking or membership protocol. pyleans needs the same: a cheap, fast path for writing grains without spinning up a cluster.

## Decision

Phase 1 ships a single-silo, in-process mode analogous to Orleans' `UseLocalhostClustering()`. Everything runs in one Python process with in-memory providers. No networking, no clustering, no membership protocol. This is the default for local development and testing.

## Consequences

- Phase 1 has no transport layer, no gateway mesh, no membership heartbeats — the runtime is small and fast.
- Tests run entirely in-process with no external services.
- The same grain code runs unchanged in Phase 2 multi-silo clusters — user-facing APIs are identical.
- **Single-silo is a scope decision, not a correctness exemption.** The single-activation contract from [adr-single-activation-cluster](adr-single-activation-cluster.md) is still in force in Phase 1; it is just trivially satisfied because there is only one silo. Phase 2 introduces the machinery (directory, cluster transport, placement) that preserves the same contract across multiple silos. No Phase 1 design choice is permitted to rely on "one silo" as a correctness argument — only as a scope argument.

## Related

- [adr-single-activation-cluster](adr-single-activation-cluster.md) — the cluster-wide contract; trivially satisfied in single-silo mode, enforced by Phase 2 subsystems in multi-silo mode.
- [adr-concurrency-model](adr-concurrency-model.md)
- [adr-library-vs-cli](adr-library-vs-cli.md)
- [adr-provider-interfaces](adr-provider-interfaces.md) — in-memory default adapters.
