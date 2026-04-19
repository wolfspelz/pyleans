"""Shared in-memory MembershipProvider for tests.

Implements the Phase 2 OCC contract in memory so every integration test
that needs a functional membership table can depend on the same double.
Also exposes observation counters (``heartbeat_count``, etc.) that tests
assert against without reaching into private state.
"""

from __future__ import annotations

from dataclasses import replace

from pyleans.identity import SiloInfo, SuspicionVote
from pyleans.providers.errors import SiloNotFoundError, TableStaleError
from pyleans.providers.membership import MembershipProvider, MembershipSnapshot


class InMemoryMembershipProvider(MembershipProvider):
    """Functional in-memory implementation of :class:`MembershipProvider`."""

    def __init__(self) -> None:
        self._version = 0
        self._silos: dict[str, SiloInfo] = {}
        self.heartbeat_count = 0

    async def read_all(self) -> MembershipSnapshot:
        return MembershipSnapshot(
            version=self._version,
            silos=[replace(s, suspicions=list(s.suspicions)) for s in self._silos.values()],
        )

    async def try_update_silo(self, silo: SiloInfo) -> SiloInfo:
        silo_id = silo.address.silo_id
        current = self._silos.get(silo_id)
        if current is None:
            if silo.etag is not None:
                raise SiloNotFoundError(
                    f"Silo {silo_id!r} not found — cannot update with etag {silo.etag!r}"
                )
        elif current.etag != silo.etag:
            raise TableStaleError(
                f"etag mismatch for {silo_id!r}: expected {silo.etag!r}, current {current.etag!r}"
            )
        self._version += 1
        if current is not None and current.i_am_alive != silo.i_am_alive:
            self.heartbeat_count += 1
        stored = replace(silo, etag=f"v{self._version}", suspicions=list(silo.suspicions))
        self._silos[silo_id] = stored
        return stored

    async def try_add_suspicion(
        self, silo_id: str, vote: SuspicionVote, expected_etag: str
    ) -> SiloInfo:
        current = self._silos.get(silo_id)
        if current is None:
            raise SiloNotFoundError(f"Silo {silo_id!r} not found")
        if current.etag != expected_etag:
            raise TableStaleError(
                f"etag mismatch for {silo_id!r}: "
                f"expected {expected_etag!r}, current {current.etag!r}"
            )
        self._version += 1
        updated = replace(
            current,
            suspicions=[*current.suspicions, vote],
            etag=f"v{self._version}",
        )
        self._silos[silo_id] = updated
        return updated

    async def try_delete_silo(self, silo: SiloInfo) -> None:
        current = self._silos.get(silo.address.silo_id)
        if current is None:
            return
        if current.etag != silo.etag:
            raise TableStaleError(
                f"etag mismatch for {silo.address.silo_id!r}: "
                f"expected {silo.etag!r}, current {current.etag!r}"
            )
        self._version += 1
        del self._silos[silo.address.silo_id]

    @property
    def silos(self) -> dict[str, SiloInfo]:
        """Observation helper: snapshot of stored rows keyed by silo_id."""
        return dict(self._silos)

    @property
    def version(self) -> int:
        return self._version
