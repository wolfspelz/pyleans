# Task 02-14: Local Directory Cache With Invalidation

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-13-distributed-grain-directory.md](task-02-13-distributed-grain-directory.md)

## References
- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) -- the cache is a performance optimisation *on top of* the directory; it must never be load-bearing for correctness. This task documents and tests that property.
- [adr-grain-directory](../adr/adr-grain-directory.md) -- the authority the cache fronts.
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §6.2 "Local caching... most grain calls resolve locally without a remote directory read"
- [plan.md](../plan.md) -- Phase 2 item 6

## Description

Without a cache, every grain call on a non-owner silo incurs a directory RPC round-trip. Orleans' answer is a per-silo cache of recent lookups, invalidated on membership change. Any cache hit means the cost of "where does this grain live?" drops from milliseconds to microseconds; cache misses fall back through to [task-02-13](task-02-13-distributed-grain-directory.md).

The cache is optional from a correctness standpoint -- never relying on the cache is always correct. That property is what we test: the cache must be safely discardable. Single-activation is enforced by the [distributed directory](task-02-13-distributed-grain-directory.md); the cache can only return stale entries, never wrong ones, because a stale entry points to a silo that either still owns the grain (correct) or rejects the call with `NotOwner` / `TransportConnectionError` (triggering invalidation + refetch).

### Files to create

- `src/pyleans/pyleans/cluster/directory_cache.py`

### Design

```python
class DirectoryCache:
    """In-process cache wrapping an IGrainDirectory.

    Cache hits skip the owner RPC. Cache entries are invalidated on:
      1. Membership change (a silo joined/left -- owner assignment may have changed).
      2. Explicit eviction when a call to the cached silo fails with
         TransportConnectionError (the entry is stale -- grain is elsewhere now).
      3. TTL expiry (belt and suspenders, default 60 seconds).

    Eviction on call failure is the mechanism that closes the gap between
    'directory says silo X hosts this grain' and 'silo X was just marked dead' —
    without it, the caller retries forever against a dead entry.
    """

    def __init__(
        self,
        inner: IGrainDirectory,
        max_size: int = 10_000,
        ttl: float = 60.0,
    ) -> None:
        self._inner = inner
        self._cache: collections.OrderedDict[GrainId, _CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl

    async def lookup(self, grain_id: GrainId) -> DirectoryEntry | None:
        entry = self._cache.get(grain_id)
        if entry is not None and not entry.is_expired():
            self._cache.move_to_end(grain_id)   # LRU touch
            return entry.value
        value = await self._inner.lookup(grain_id)
        if value is not None:
            self._put(grain_id, value)
        return value

    async def resolve_or_activate(self, grain_id, placement, caller) -> DirectoryEntry:
        entry = self._cache.get(grain_id)
        if entry is not None and not entry.is_expired():
            return entry.value
        value = await self._inner.resolve_or_activate(grain_id, placement, caller)
        self._put(grain_id, value)
        return value

    async def register(self, grain_id, silo) -> DirectoryEntry:
        value = await self._inner.register(grain_id, silo)
        self._put(grain_id, value)
        return value

    async def unregister(self, grain_id, silo) -> None:
        await self._inner.unregister(grain_id, silo)
        self._cache.pop(grain_id, None)

    def invalidate(self, grain_id: GrainId) -> None:
        """Evict a single entry. Called by the runtime when a grain call to
        the cached silo fails with TransportConnectionError."""
        self._cache.pop(grain_id, None)

    def invalidate_all(self) -> None:
        """Membership-change hook — nuke every entry."""
        self._cache.clear()

    def invalidate_silo(self, silo: SiloAddress) -> None:
        """Finer-grained: drop entries that point to this silo. Used when
        `silo` disconnects to avoid unnecessary lookups for grains hosted
        elsewhere."""
        for g in [gid for gid, e in self._cache.items() if e.value.silo == silo]:
            self._cache.pop(g)
```

### Invalidation triggers

1. **Membership change** -- the `MembershipAgent` ([task-02-11](task-02-11-failure-detector.md)) calls `cache.invalidate_all()` whenever the active-silo set changes. Cheaper than recomputing which entries moved; membership changes are rare.
2. **Transport connection loss** -- `IClusterTransport.on_connection_lost(silo, err)` callback calls `cache.invalidate_silo(silo)`. Entries pointing to the disconnected silo are now suspect; re-resolve on next access.
3. **TTL** -- each entry has `expires_at = monotonic() + ttl`. Belt-and-suspenders against missed invalidation events.
4. **Per-call failure** -- the runtime in [task-02-16](task-02-16-remote-grain-invoke.md) catches `TransportConnectionError` from a grain call, calls `cache.invalidate(grain_id)`, and retries exactly once before failing the caller.

### Cache size bound

`max_size` exists to bound memory for long-running silos that touch many grains. Eviction policy: LRU (hence `OrderedDict`). At 10_000 entries × ~200 bytes each, the cache caps at ~2 MB -- negligible, but configurable for operators who want a smaller footprint.

### Correctness under race

Two subtle races to cover explicitly:

1. **Lookup during invalidation**: `lookup()` is midway through awaiting `_inner.lookup()` when `invalidate_all()` runs. The result of the pending lookup is still inserted into the (now-empty) cache. Acceptable -- the entry is correct as of the RPC time, and any stale-read will be caught by a subsequent invalidation. No locking required; verified by test.

2. **Put-over-put**: two concurrent `resolve_or_activate` calls for the same `grain_id` both get a response and both call `_put`. Since the directory guarantees single-activation, both responses carry the same `DirectoryEntry` -- second put is harmless. Test case exists.

### Why not a CachedGrainDirectory class that wraps and replaces?

The cache is a decorator over `IGrainDirectory`. Making it a separate class and composing (`DirectoryCache(DistributedGrainDirectory(...))`) is the straight SOLID move: single responsibility (caching vs lookups), open/closed (cache can wrap any directory implementation including the test-local one). Silo startup in [task-02-17](task-02-17-silo-lifecycle-stages.md) wires them: `cache = DirectoryCache(dist_dir)` and the runtime uses `cache`.

### Acceptance criteria

- [ ] Cache hit skips the inner directory entirely (verified by instrumenting inner `lookup`)
- [ ] Cache miss falls through, populates, returns correct value
- [ ] TTL expiry forces a re-fetch
- [ ] `invalidate(g)` drops the entry; next lookup hits the inner directory
- [ ] `invalidate_all()` clears every entry
- [ ] `invalidate_silo(s)` drops only entries pointing to `s`
- [ ] LRU eviction kicks in at `max_size`; least-recently-used entry removed
- [ ] Safe under concurrent invalidate + lookup (race test)
- [ ] Integration test: 3-silo cluster, same `GrainId` called 100 times from a single silo -- only first call hits the remote directory
- [ ] Unit tests cover all six invalidation paths, TTL, LRU, race

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
