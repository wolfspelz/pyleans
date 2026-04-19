"""Single-silo in-memory :class:`IGrainDirectory` adapter.

Used in Phase 1 / dev mode and as the fallback when no distributed
directory is wired into the silo. Matches the port contract exactly
so every runtime test against :class:`IGrainDirectory` applies here
unchanged.
"""

from __future__ import annotations

import asyncio
import logging

from pyleans.cluster.directory import DirectoryEntry, IGrainDirectory
from pyleans.cluster.placement import PlacementStrategy
from pyleans.identity import GrainId, SiloAddress

logger = logging.getLogger(__name__)


class LocalGrainDirectory(IGrainDirectory):
    """In-memory directory bound to a single silo.

    ``resolve_or_activate`` always self-assigns because the only
    active silo in this configuration is the local one. The placement
    strategy is still consulted so the code path matches the
    distributed implementation — the assert documents the invariant.
    """

    def __init__(self, local_silo: SiloAddress) -> None:
        self._local = local_silo
        self._entries: dict[GrainId, DirectoryEntry] = {}
        self._epoch = 0
        self._lock = asyncio.Lock()

    @property
    def local_silo(self) -> SiloAddress:
        return self._local

    async def register(self, grain_id: GrainId, silo: SiloAddress) -> DirectoryEntry:
        async with self._lock:
            existing = self._entries.get(grain_id)
            if existing is not None:
                return existing
            self._epoch += 1
            entry = DirectoryEntry(grain_id=grain_id, silo=silo, activation_epoch=self._epoch)
            self._entries[grain_id] = entry
            logger.debug(
                "Registered %s on %s (epoch=%d)",
                grain_id,
                silo.silo_id,
                self._epoch,
            )
            return entry

    async def lookup(self, grain_id: GrainId) -> DirectoryEntry | None:
        return self._entries.get(grain_id)

    async def unregister(self, grain_id: GrainId, silo: SiloAddress) -> None:
        async with self._lock:
            entry = self._entries.get(grain_id)
            if entry is None:
                return
            if entry.silo != silo:
                logger.debug(
                    "Ignoring unregister of %s from %s: owner is %s",
                    grain_id,
                    silo.silo_id,
                    entry.silo.silo_id,
                )
                return
            del self._entries[grain_id]
            logger.debug("Unregistered %s from %s", grain_id, silo.silo_id)

    async def resolve_or_activate(
        self,
        grain_id: GrainId,
        placement: PlacementStrategy,
        caller: SiloAddress | None,
    ) -> DirectoryEntry:
        existing = self._entries.get(grain_id)
        if existing is not None:
            return existing
        chosen = placement.pick_silo(grain_id, caller, [self._local])
        if chosen != self._local:
            raise RuntimeError(
                f"LocalGrainDirectory picked non-local silo {chosen.silo_id!r}; "
                "placement strategy violated single-silo invariant",
            )
        return await self.register(grain_id, self._local)
