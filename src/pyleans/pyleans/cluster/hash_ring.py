"""Consistent hash ring with virtual nodes.

The ring is a pure data structure — no networking, no membership coupling.
It takes a list of silos as input and answers "who owns this key?" and
"who are my N successors?" deterministically. Every silo that builds the
ring from the same input produces identical answers.

The ring is the shared primitive consumed by the failure detector
(probe-target selection) and the distributed grain directory
(partition-owner selection). See
:doc:`../../../docs/adr/adr-grain-directory` for the rationale.

Construction is O(N·V log N·V) (sort); lookups are O(log N·V)
(binary search). The ring is rebuilt on every membership change rather
than incrementally maintained — N stays small and rebuilding avoids
subtle bugs under churn.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass

from pyleans.cluster.identity import hash_silo_virtual_node
from pyleans.identity import SiloAddress

VIRTUAL_NODES_PER_SILO = 30

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RingPosition:
    """One virtual-node position on the ring.

    ``position`` is a 64-bit unsigned hash; ``silo`` is the physical silo
    owning this virtual node; ``vnode_index`` is the virtual-node index
    within that silo (0 <= index < VIRTUAL_NODES_PER_SILO).
    """

    position: int
    silo: SiloAddress
    vnode_index: int


class ConsistentHashRing:
    """Immutable consistent hash ring with virtual nodes."""

    def __init__(
        self,
        silos: list[SiloAddress],
        virtual_nodes_per_silo: int = VIRTUAL_NODES_PER_SILO,
    ) -> None:
        if virtual_nodes_per_silo < 1:
            raise ValueError(f"virtual_nodes_per_silo must be >= 1, got {virtual_nodes_per_silo}")
        deduped = list(dict.fromkeys(silos))
        self._silos: list[SiloAddress] = sorted(deduped, key=lambda s: s.silo_id)
        self._virtual_nodes_per_silo = virtual_nodes_per_silo
        self._positions: list[RingPosition] = self._build_positions()
        self._sorted_hashes: list[int] = [p.position for p in self._positions]
        _log.debug(
            "built hash ring: silos=%d vnodes_per_silo=%d total_positions=%d",
            len(self._silos),
            virtual_nodes_per_silo,
            len(self._positions),
        )

    def _build_positions(self) -> list[RingPosition]:
        positions: list[RingPosition] = []
        for silo in self._silos:
            for vnode_index in range(self._virtual_nodes_per_silo):
                position = hash_silo_virtual_node(silo, vnode_index)
                positions.append(
                    RingPosition(position=position, silo=silo, vnode_index=vnode_index)
                )
        positions.sort(key=lambda p: (p.position, p.silo.silo_id, p.vnode_index))
        return positions

    def owner_of(self, key_hash: int) -> SiloAddress | None:
        """Return the silo owning the arc containing ``key_hash``.

        Walks clockwise: returns the silo at the first position >= ``key_hash``,
        wrapping to the lowest position if ``key_hash`` exceeds the max.
        Returns ``None`` only when the ring is empty.
        """
        if not self._positions:
            return None
        index = bisect.bisect_left(self._sorted_hashes, key_hash)
        if index == len(self._positions):
            index = 0
        return self._positions[index].silo

    def successors(self, silo: SiloAddress, count: int) -> list[SiloAddress]:
        """Return the next ``count`` distinct successor silos on the ring.

        Used by the failure detector to pick probe targets. If ``count`` is
        larger than the number of other silos on the ring, returns every
        other silo. Order is deterministic: same input, same output on
        every silo.
        """
        if count <= 0 or silo not in self._silos or len(self._silos) <= 1:
            return []
        start_index = self._first_vnode_index(silo)
        if start_index is None:
            return []
        result: list[SiloAddress] = []
        seen: set[SiloAddress] = {silo}
        total = len(self._positions)
        for offset in range(1, total + 1):
            candidate = self._positions[(start_index + offset) % total].silo
            if candidate in seen:
                continue
            result.append(candidate)
            seen.add(candidate)
            if len(result) == count:
                break
        return result

    def _first_vnode_index(self, silo: SiloAddress) -> int | None:
        for index, position in enumerate(self._positions):
            if position.silo == silo:
                return index
        return None

    @property
    def silos(self) -> list[SiloAddress]:
        """Snapshot of the silos currently on the ring, sorted by ``silo_id``."""
        return list(self._silos)

    @property
    def positions(self) -> list[RingPosition]:
        """Snapshot of every virtual-node position on the ring, sorted by hash."""
        return list(self._positions)

    def __len__(self) -> int:
        return len(self._silos)

    def __contains__(self, silo: object) -> bool:
        return silo in self._silos
