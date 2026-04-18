# Consistent Hash Ring with Virtual Nodes — Phase 2 Grain Placement

This document explains the placement mechanism pyleans adopts in Phase 2 for distributing grain activations across silos: a **consistent hash ring** with **30 virtual nodes per silo**. The decision itself is recorded in [adr-grain-directory](../adr/adr-grain-directory.md); this document is the "what is it, and why does it look like this" reference.

## The problem it solves

In a multi-silo cluster, every grain (`PlayerGrain("42")`, `RoomGrain("lobby")`) needs to live on *exactly one* silo at a time. When a client calls a grain, the runtime must decide: which silo hosts it? Every silo and every client must agree on that decision — otherwise you get two activations on different silos, which breaks the Virtual Actor guarantee that a grain behaves like a single logical instance.

A plain `hash(grain_id) % N` placement works, but the moment a silo joins or leaves the cluster (N changes), **almost every grain gets remapped**. That means mass deactivation and re-activation across the cluster on every membership change. Unusable at scale.

## Consistent hashing

Picture a circle — the "ring" — with positions 0 to 2³²-1. Each silo is placed on the ring at position `hash(silo_address)`. Each grain is placed at position `hash(grain_id)`. A grain is owned by the first silo you meet walking clockwise from the grain's position.

```
                ring positions
                    0
              ╭─────┼─────╮
          silo A  ·   ·   ·  silo B
              ·   ·   ·   ·
              · grain G ·
              ·   ↓     ·
              ·  owns   ·
              ·         ·
               ·  silo B ·
                 ╰──────╯
```

When a silo is added or removed, **only the grains in its arc of the ring move**. On average that's `1/N` of all grains — not "almost all."

### Why "consistent"?

The word "consistent" here is the CS-research term of art (Karger et al., 1997): it means adding or removing a bucket remaps *O(K/N)* keys rather than *O(K)*. It is not the same "consistent" as in "strong consistency"; the ring by itself says nothing about who sees what when.

## Why virtual nodes?

Plain consistent hashing has a load-balance problem. With N=3 silos, by bad luck one silo might land on a position where it covers 60% of the ring and another 20%. Traffic is then grossly uneven, and grain load follows.

The fix: each *physical* silo places *V virtual nodes* on the ring, one per hash:

```
virtual positions for silo A = [ hash("A:0"), hash("A:1"), …, hash("A:29") ]
```

With V=30 per silo, each physical silo ends up with many small arcs scattered around the ring. By the law of large numbers the arc lengths that sum to each physical silo's share of the ring converge on `1/N` with small variance.

**30** is a common choice (Orleans, DynamoDB, Cassandra use similar numbers): enough virtual nodes to smooth out the variance, not so many that ring lookups get expensive or the membership table balloons.

## What it's good for in pyleans

1. **Stable grain placement under membership churn.** Silo restart, scale-up, scale-down, crash recovery — only ~1/N of activations have to migrate instead of all of them.
2. **No coordinator.** Every silo computes the ring locally from the membership table and reaches the same answer. Eventually consistent, but no central service to be a bottleneck or single point of failure.
3. **Prefer-local routing opportunity.** Orleans-style placement can check "is this grain's home silo me?" with a pure hash lookup, no network round-trip.
4. **Even load** across silos without manual sharding, partition assignments, or operator tuning.

## Cost and caveats

- **Brief double-activation windows.** During membership changes, different silos may temporarily disagree on ownership. The same grain can be activated on two silos for a short window until every silo has observed the membership update. This is the "eventually consistent" caveat from [adr-grain-directory](../adr/adr-grain-directory.md). The runtime is expected to detect and resolve these duplicates.
- **Ring lookup cost.** Each grain call does a binary search on `N·V` positions. For a realistic cluster (N=10, V=30) that's 300 entries — trivial. The ring itself is cached per silo and rebuilt on membership change.
- **Hash quality matters.** A weak hash produces clumping even with virtual nodes. Use a good non-cryptographic hash (e.g. `xxhash`, or a carefully-chosen slice of SHA-256). `hash()` from Python's builtins is process-local and not safe here.

## Relation to Orleans

Orleans pre-9.0 used this exact model. Orleans 9.0+ moved to a strongly consistent grain directory (effectively a consensus protocol under the hood) that eliminates the brief double-activation window. pyleans tracks the pre-9.0 model for the PoC because it avoids consensus machinery while keeping the important properties (stability under churn, no coordinator, even load). Revisiting this choice post-PoC is called out in [adr-grain-directory](../adr/adr-grain-directory.md).

## Related

- [adr-grain-directory](../adr/adr-grain-directory.md) — the decision to adopt this model.
- [plan.md §5 Phase 2](../plan.md) — where the ring is introduced in the roadmap.
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) — Orleans' own directory documentation.
