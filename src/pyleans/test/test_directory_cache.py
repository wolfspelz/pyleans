"""Tests for :mod:`pyleans.cluster.directory_cache`.

Covers the acceptance criteria from task-02-14: hit / miss /
repopulation, TTL expiry, per-grain / cluster-wide / per-silo
invalidation, LRU eviction at ``max_size``, race tolerance for
concurrent lookup + invalidate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from pyleans.cluster.directory import DirectoryEntry, IGrainDirectory
from pyleans.cluster.directory_cache import DirectoryCache
from pyleans.cluster.placement import PlacementStrategy, PreferLocalPlacement
from pyleans.identity import GrainId, SiloAddress


def _silo(host: str = "s1") -> SiloAddress:
    return SiloAddress(host=host, port=11111, epoch=1)


def _grain(key: str = "k1") -> GrainId:
    return GrainId("Counter", key)


@dataclass
class FakeDirectory(IGrainDirectory):
    """Records every call for cache-behaviour assertions."""

    silo_owner: SiloAddress = field(default_factory=_silo)
    lookups: int = 0
    registers: int = 0
    resolves: int = 0
    unregisters: int = 0
    entries: dict[GrainId, DirectoryEntry] = field(default_factory=dict)

    async def register(self, grain_id: GrainId, silo: SiloAddress) -> DirectoryEntry:
        self.registers += 1
        existing = self.entries.get(grain_id)
        if existing is not None:
            return existing
        entry = DirectoryEntry(grain_id, silo, len(self.entries) + 1)
        self.entries[grain_id] = entry
        return entry

    async def lookup(self, grain_id: GrainId) -> DirectoryEntry | None:
        self.lookups += 1
        return self.entries.get(grain_id)

    async def unregister(self, grain_id: GrainId, silo: SiloAddress) -> None:
        self.unregisters += 1
        current = self.entries.get(grain_id)
        if current is not None and current.silo == silo:
            del self.entries[grain_id]

    async def resolve_or_activate(
        self,
        grain_id: GrainId,
        placement: PlacementStrategy,
        caller: SiloAddress | None,
    ) -> DirectoryEntry:
        del placement, caller
        self.resolves += 1
        existing = self.entries.get(grain_id)
        if existing is not None:
            return existing
        entry = DirectoryEntry(grain_id, self.silo_owner, len(self.entries) + 1)
        self.entries[grain_id] = entry
        return entry


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestConstruction:
    def test_rejects_non_positive_max_size(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="max_size"):
            DirectoryCache(FakeDirectory(), max_size=0)

    def test_rejects_non_positive_ttl(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="ttl"):
            DirectoryCache(FakeDirectory(), ttl=0)


class TestHitMiss:
    async def test_first_lookup_is_a_miss(self) -> None:
        # Arrange
        inner = FakeDirectory()
        await inner.register(_grain(), _silo())
        cache = DirectoryCache(inner)

        # Act
        value = await cache.lookup(_grain())

        # Assert
        assert value is not None
        assert inner.lookups == 1
        assert cache.contains(_grain())

    async def test_second_lookup_is_a_hit(self) -> None:
        # Arrange
        inner = FakeDirectory()
        await inner.register(_grain(), _silo())
        cache = DirectoryCache(inner)
        await cache.lookup(_grain())

        # Act
        value = await cache.lookup(_grain())

        # Assert - inner only called once
        assert value is not None
        assert inner.lookups == 1

    async def test_resolve_caches_result(self) -> None:
        # Arrange
        inner = FakeDirectory()
        cache = DirectoryCache(inner)
        first = await cache.resolve_or_activate(_grain(), PreferLocalPlacement(), caller=_silo())

        # Act
        second = await cache.resolve_or_activate(_grain(), PreferLocalPlacement(), caller=_silo())

        # Assert
        assert second == first
        assert inner.resolves == 1

    async def test_register_caches_result(self) -> None:
        # Arrange
        inner = FakeDirectory()
        cache = DirectoryCache(inner)
        await cache.register(_grain(), _silo())

        # Act
        found = await cache.lookup(_grain())

        # Assert - no additional inner lookup call
        assert found is not None
        assert inner.lookups == 0


class TestTTL:
    async def test_ttl_expiry_forces_refetch(self) -> None:
        # Arrange
        clock = _Clock()
        inner = FakeDirectory()
        await inner.register(_grain(), _silo())
        cache = DirectoryCache(inner, ttl=10.0, clock=clock)
        await cache.lookup(_grain())
        clock.now += 11.0

        # Act
        await cache.lookup(_grain())

        # Assert - fell through to inner a second time
        assert inner.lookups == 2


class TestExplicitInvalidation:
    async def test_invalidate_drops_entry(self) -> None:
        # Arrange
        inner = FakeDirectory()
        await inner.register(_grain(), _silo())
        cache = DirectoryCache(inner)
        await cache.lookup(_grain())

        # Act
        cache.invalidate(_grain())

        # Assert
        assert not cache.contains(_grain())

    async def test_invalidate_all_clears_cache(self) -> None:
        # Arrange
        inner = FakeDirectory()
        cache = DirectoryCache(inner)
        for i in range(3):
            await cache.resolve_or_activate(_grain(f"k{i}"), PreferLocalPlacement(), caller=_silo())

        # Act
        cache.invalidate_all()

        # Assert
        assert cache.size == 0

    async def test_invalidate_silo_drops_only_matching_entries(self) -> None:
        # Arrange
        inner = FakeDirectory()
        s_kept = _silo("kept")
        s_dropped = _silo("dropped")
        inner.entries[_grain("a")] = DirectoryEntry(_grain("a"), s_kept, 1)
        inner.entries[_grain("b")] = DirectoryEntry(_grain("b"), s_dropped, 2)
        cache = DirectoryCache(inner)
        await cache.lookup(_grain("a"))
        await cache.lookup(_grain("b"))

        # Act
        cache.invalidate_silo(s_dropped)

        # Assert
        assert cache.contains(_grain("a"))
        assert not cache.contains(_grain("b"))

    async def test_unregister_drops_cache_entry(self) -> None:
        # Arrange
        inner = FakeDirectory()
        await inner.register(_grain(), _silo())
        cache = DirectoryCache(inner)
        await cache.lookup(_grain())

        # Act
        await cache.unregister(_grain(), _silo())

        # Assert
        assert not cache.contains(_grain())


class TestLRUEviction:
    async def test_max_size_bound_evicts_oldest(self) -> None:
        # Arrange
        inner = FakeDirectory()
        cache = DirectoryCache(inner, max_size=3)
        for i in range(4):
            await cache.resolve_or_activate(_grain(f"k{i}"), PreferLocalPlacement(), caller=_silo())

        # Act / Assert - first entry evicted under LRU
        assert not cache.contains(_grain("k0"))
        assert cache.contains(_grain("k1"))
        assert cache.contains(_grain("k2"))
        assert cache.contains(_grain("k3"))
        assert cache.size == 3

    async def test_lookup_touches_recency(self) -> None:
        # Arrange
        inner = FakeDirectory()
        cache = DirectoryCache(inner, max_size=3)
        for i in range(3):
            await cache.resolve_or_activate(_grain(f"k{i}"), PreferLocalPlacement(), caller=_silo())

        # Touch oldest so it becomes MRU
        await cache.lookup(_grain("k0"))

        # Act - adding a fourth entry evicts k1 (now oldest)
        await cache.resolve_or_activate(_grain("k3"), PreferLocalPlacement(), caller=_silo())

        # Assert
        assert cache.contains(_grain("k0"))
        assert not cache.contains(_grain("k1"))


class TestRaceSafety:
    async def test_concurrent_resolves_converge_on_same_entry(self) -> None:
        # Arrange
        inner = FakeDirectory()
        cache = DirectoryCache(inner)

        async def hit() -> DirectoryEntry:
            return await cache.resolve_or_activate(_grain(), PreferLocalPlacement(), caller=_silo())

        # Act
        results = await asyncio.gather(*(hit() for _ in range(10)))

        # Assert - all identical; the fake returned the same entry each call,
        # so the cache has a single consistent value.
        assert all(r == results[0] for r in results)

    async def test_invalidate_all_during_inflight_lookup_is_safe(self) -> None:
        # Arrange - inner lookup is slow so we can invalidate mid-flight
        inner = FakeDirectory()
        await inner.register(_grain(), _silo())

        slow_started = asyncio.Event()
        slow_release = asyncio.Event()

        orig_lookup = inner.lookup

        async def slow_lookup(grain_id: GrainId) -> DirectoryEntry | None:
            slow_started.set()
            await slow_release.wait()
            return await orig_lookup(grain_id)

        inner.lookup = slow_lookup  # type: ignore[method-assign]
        cache = DirectoryCache(inner)

        # Act - start lookup; mid-flight, invalidate_all
        task = asyncio.create_task(cache.lookup(_grain()))
        await slow_started.wait()
        cache.invalidate_all()
        slow_release.set()
        result = await task

        # Assert - lookup still succeeds and result is cached (by design)
        assert result is not None
