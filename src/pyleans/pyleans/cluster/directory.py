"""Grain directory port — silo-authoritative mapping from :class:`GrainId` to host silo.

The directory is one of the three subsystems (membership, cluster
transport, directory) that jointly enforce the single-activation
contract from :doc:`../../../docs/adr/adr-single-activation-cluster`.

The ABC lives in this module so every adapter — ``LocalGrainDirectory``
today, ``DistributedGrainDirectory`` in task-02-13 — satisfies the
same contract; tests written against the ABC apply to every adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from pyleans.cluster.placement import PlacementStrategy
from pyleans.identity import GrainId, SiloAddress


@dataclass(frozen=True)
class DirectoryEntry:
    """Pointer to the unique activation of ``grain_id`` in the cluster.

    ``activation_epoch`` is a silo-local monotonic counter: if a grain
    reactivates on the same silo after an idle eviction, the new
    entry gets a higher ``activation_epoch`` so callers holding a
    stale reference can detect the change.
    """

    grain_id: GrainId
    silo: SiloAddress
    activation_epoch: int


class IGrainDirectory(ABC):
    """Port: which silo hosts an activation of a given :class:`GrainId`, cluster-wide.

    Contract:

    * Register-if-absent: :meth:`register` of an already-present grain
      returns the existing entry, not an error. The caller's preferred
      silo wins only if no entry exists.
    * :meth:`unregister` removes an entry only when the caller-supplied
      ``silo`` matches the recorded owner. No-op otherwise.
    * :meth:`resolve_or_activate` is the fused find-or-allocate path
      every runtime takes before dispatching a call. Placement decides
      ownership when the grain is unregistered; the directory decides
      *when* to consult placement (exactly once, under its
      single-activation lock).
    """

    @abstractmethod
    async def register(self, grain_id: GrainId, silo: SiloAddress) -> DirectoryEntry:
        """Register-if-absent.

        If ``grain_id`` is already registered (possibly on a different
        silo), returns the existing entry. The caller's preferred
        ``silo`` wins only if no entry exists.
        """

    @abstractmethod
    async def lookup(self, grain_id: GrainId) -> DirectoryEntry | None:
        """Return the registered entry or ``None`` if not registered."""

    @abstractmethod
    async def unregister(self, grain_id: GrainId, silo: SiloAddress) -> None:
        """Remove the entry iff it currently maps to ``silo``.

        No-op if the entry is absent or maps to a different silo —
        the directory never performs cross-silo eviction.
        """

    @abstractmethod
    async def resolve_or_activate(
        self,
        grain_id: GrainId,
        placement: PlacementStrategy,
        caller: SiloAddress | None,
    ) -> DirectoryEntry:
        """Atomic find-or-allocate.

        Returns the directory entry the caller should route to. If the
        grain is unregistered, the directory consults ``placement``
        with ``caller`` and the cluster's active silo set to pick a
        home, then registers it.
        """
