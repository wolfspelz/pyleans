"""In-process cache over an :class:`IGrainDirectory`.

The cache is a decorator: it wraps any directory implementation and
short-circuits lookups that hit recently. Correctness remains with
the inner directory — the cache is optional from a correctness
standpoint (see :doc:`../../../docs/adr/adr-single-activation-cluster`).

Invalidation triggers:

1. Membership change → :meth:`invalidate_all`.
2. Transport connection loss for a silo → :meth:`invalidate_silo`.
3. Explicit per-grain eviction on failed call → :meth:`invalidate`.
4. TTL expiry — belt-and-suspenders default 60 s.

Size-bounded by LRU at ``max_size`` entries.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from pyleans.cluster.directory import DirectoryEntry, IGrainDirectory
from pyleans.cluster.placement import PlacementStrategy
from pyleans.identity import GrainId, SiloAddress

logger = logging.getLogger(__name__)

_DEFAULT_MAX_SIZE: Final[int] = 10_000
_DEFAULT_TTL: Final[float] = 60.0


@dataclass
class _CacheEntry:
    value: DirectoryEntry
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


class DirectoryCache(IGrainDirectory):
    """LRU cache over :class:`IGrainDirectory` with multi-path invalidation."""

    def __init__(
        self,
        inner: IGrainDirectory,
        *,
        max_size: int = _DEFAULT_MAX_SIZE,
        ttl: float = _DEFAULT_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_size <= 0:
            raise ValueError(f"max_size must be > 0, got {max_size!r}")
        if ttl <= 0:
            raise ValueError(f"ttl must be > 0, got {ttl!r}")
        self._inner = inner
        self._max_size = max_size
        self._ttl = ttl
        self._clock = clock
        self._cache: OrderedDict[GrainId, _CacheEntry] = OrderedDict()

    @property
    def size(self) -> int:
        return len(self._cache)

    @property
    def inner(self) -> IGrainDirectory:
        return self._inner

    def contains(self, grain_id: GrainId) -> bool:
        entry = self._cache.get(grain_id)
        if entry is None:
            return False
        return not entry.is_expired(self._clock())

    async def register(self, grain_id: GrainId, silo: SiloAddress) -> DirectoryEntry:
        value = await self._inner.register(grain_id, silo)
        self._put(grain_id, value)
        return value

    async def lookup(self, grain_id: GrainId) -> DirectoryEntry | None:
        cached = self._cache.get(grain_id)
        if cached is not None and not cached.is_expired(self._clock()):
            self._cache.move_to_end(grain_id)
            return cached.value
        if cached is not None:
            # TTL-expired entry — drop before falling through
            self._cache.pop(grain_id, None)
        value = await self._inner.lookup(grain_id)
        if value is not None:
            self._put(grain_id, value)
        return value

    async def unregister(self, grain_id: GrainId, silo: SiloAddress) -> None:
        try:
            await self._inner.unregister(grain_id, silo)
        finally:
            self._cache.pop(grain_id, None)

    async def resolve_or_activate(
        self,
        grain_id: GrainId,
        placement: PlacementStrategy,
        caller: SiloAddress | None,
    ) -> DirectoryEntry:
        cached = self._cache.get(grain_id)
        if cached is not None and not cached.is_expired(self._clock()):
            self._cache.move_to_end(grain_id)
            return cached.value
        if cached is not None:
            self._cache.pop(grain_id, None)
        value = await self._inner.resolve_or_activate(grain_id, placement, caller)
        self._put(grain_id, value)
        return value

    def invalidate(self, grain_id: GrainId) -> None:
        """Evict one entry — the runtime's single-retry eviction path."""
        if self._cache.pop(grain_id, None) is not None:
            logger.debug("directory cache invalidated %s", grain_id)

    def invalidate_all(self) -> None:
        """Membership-change hook — drop every entry."""
        if self._cache:
            logger.debug("directory cache invalidate_all: %d entries", len(self._cache))
        self._cache.clear()

    def invalidate_silo(self, silo: SiloAddress) -> None:
        """Drop only entries pointing to ``silo`` — transport disconnect hook."""
        stale = [gid for gid, e in self._cache.items() if e.value.silo == silo]
        for gid in stale:
            self._cache.pop(gid, None)
        if stale:
            logger.debug(
                "directory cache invalidate_silo %s: dropped %d entries",
                silo.silo_id,
                len(stale),
            )

    def _put(self, grain_id: GrainId, value: DirectoryEntry) -> None:
        expires_at = self._clock() + self._ttl
        existing = self._cache.get(grain_id)
        if existing is not None:
            existing.value = value
            existing.expires_at = expires_at
            self._cache.move_to_end(grain_id)
            return
        self._cache[grain_id] = _CacheEntry(value=value, expires_at=expires_at)
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
