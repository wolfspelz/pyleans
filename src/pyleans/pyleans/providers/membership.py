"""Membership provider ABC — port for silo discovery and health.

The provider exposes four primitives — :meth:`read_all`,
:meth:`read_silo`, :meth:`try_update_silo`, :meth:`try_add_suspicion` —
that together implement the Orleans-style OCC table. Higher-level
operations (register, heartbeat, shutdown) are performed by the caller
on top of these primitives; the ABC deliberately does not offer
one-shot helpers that would hide the read-modify-write pattern every
real write must follow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace

from pyleans.identity import SiloInfo, SuspicionVote


@dataclass(frozen=True)
class MembershipSnapshot:
    """Atomic read view of the membership table.

    ``version`` is monotonic non-decreasing; every mutating write
    increments it. ``silos`` is a list of :class:`SiloInfo` rows — each
    carrying its own row-level ``etag`` for optimistic-concurrency
    writes. All rows in one snapshot are consistent with the same
    ``version`` (file providers achieve this by reading once; SQL
    providers by reading under one transaction).
    """

    version: int
    silos: list[SiloInfo] = field(default_factory=list)


class MembershipProvider(ABC):
    """Pluggable membership-table port backed by OCC primitives."""

    @abstractmethod
    async def read_all(self) -> MembershipSnapshot:
        """Return the whole table as a consistent snapshot."""

    @abstractmethod
    async def try_update_silo(self, silo: SiloInfo) -> SiloInfo:
        """Compare-and-swap the row identified by ``silo.address.silo_id``.

        ``silo.etag`` carries the expected pre-image etag. When the etag
        is ``None`` the call is interpreted as "create"; when the row
        already exists with a different etag the call raises
        :class:`TableStaleError`. The method additionally increments the
        table version atomically with the row update.

        Returns the freshly-written :class:`SiloInfo` with its new
        ``etag`` attached.
        """

    @abstractmethod
    async def try_add_suspicion(
        self, silo_id: str, vote: SuspicionVote, expected_etag: str
    ) -> SiloInfo:
        """Atomically append ``vote`` to the suspicions list on ``silo_id``.

        Raises :class:`TableStaleError` if the row's current etag does
        not match ``expected_etag``.
        """

    @abstractmethod
    async def try_delete_silo(self, silo: SiloInfo) -> None:
        """Remove the row identified by ``silo.address.silo_id``.

        ``silo.etag`` carries the expected pre-image etag. When the row
        has been re-written in the meantime the call raises
        :class:`TableStaleError`; the caller retries.
        """

    async def read_silo(self, silo_id: str) -> SiloInfo | None:
        """Return the row for ``silo_id`` or ``None`` if absent.

        Default implementation filters :meth:`read_all`; providers with
        a single-row read primitive (SQL ``WHERE silo_id=?``) should
        override for the hot failure-detector path.
        """
        snapshot = await self.read_all()
        for silo in snapshot.silos:
            if silo.address.silo_id == silo_id:
                return silo
        return None


def with_replacements(silo: SiloInfo, **replacements: object) -> SiloInfo:
    """Return a copy of ``silo`` with the named fields replaced.

    Wraps :func:`dataclasses.replace` and copies the mutable
    ``suspicions`` list so callers never accidentally share references.
    """
    copied = replace(silo, suspicions=list(silo.suspicions))
    return replace(copied, **replacements)  # type: ignore[arg-type]
