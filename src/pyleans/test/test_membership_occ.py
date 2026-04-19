"""Tests for the Phase 2 membership-provider OCC contract.

Exercises the new primitives (``read_all``, ``read_silo``,
``try_update_silo``, ``try_add_suspicion``) against both YAML and
Markdown implementations via parameterised fixtures.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus, SuspicionVote
from pyleans.providers.errors import SiloNotFoundError, TableStaleError
from pyleans.providers.membership import MembershipProvider, MembershipSnapshot
from pyleans.server.providers.markdown_table_membership import MarkdownTableMembershipProvider
from pyleans.server.providers.yaml_membership import YamlMembershipProvider


def _silo(host: str = "10.0.0.1", port: int = 11111, epoch: int = 1_700_000_000) -> SiloInfo:
    return SiloInfo(
        address=SiloAddress(host=host, port=port, epoch=epoch),
        status=SiloStatus.ACTIVE,
        last_heartbeat=time.time(),
        start_time=time.time(),
        cluster_id="demo",
        gateway_port=30000,
    )


@pytest.fixture(params=["yaml", "markdown"])
def provider(request: pytest.FixtureRequest, tmp_path: Path) -> MembershipProvider:
    if request.param == "yaml":
        return YamlMembershipProvider(file_path=str(tmp_path / "membership.yaml"))
    return MarkdownTableMembershipProvider(file_path=str(tmp_path / "membership.md"))


class TestSnapshot:
    async def test_empty_snapshot_has_version_zero(self, provider: MembershipProvider) -> None:
        # Act
        snapshot = await provider.read_all()

        # Assert
        assert snapshot == MembershipSnapshot(version=0, silos=[])

    async def test_register_increments_version(self, provider: MembershipProvider) -> None:
        # Arrange
        await provider.try_update_silo(_silo())

        # Act
        snapshot = await provider.read_all()

        # Assert
        assert snapshot.version == 1
        assert len(snapshot.silos) == 1

    async def test_snapshot_silos_carry_etag(self, provider: MembershipProvider) -> None:
        # Arrange
        await provider.try_update_silo(_silo())

        # Act
        snapshot = await provider.read_all()

        # Assert
        assert snapshot.silos[0].etag is not None


class TestTryUpdateSilo:
    async def test_create_with_none_etag_succeeds(self, provider: MembershipProvider) -> None:
        # Arrange
        silo = _silo()

        # Act
        written = await provider.try_update_silo(silo)

        # Assert
        assert written.etag is not None
        assert written.address == silo.address

    async def test_create_with_non_none_etag_raises_silo_not_found(
        self, provider: MembershipProvider
    ) -> None:
        # Arrange
        silo = _silo()
        silo.etag = "not-there"

        # Act / Assert
        with pytest.raises(SiloNotFoundError):
            await provider.try_update_silo(silo)

    async def test_update_with_matching_etag_succeeds(self, provider: MembershipProvider) -> None:
        # Arrange
        silo = await provider.try_update_silo(_silo())
        silo.status = SiloStatus.SHUTTING_DOWN

        # Act
        written = await provider.try_update_silo(silo)

        # Assert
        assert written.status == SiloStatus.SHUTTING_DOWN
        assert written.etag != silo.etag

    async def test_update_with_stale_etag_raises(self, provider: MembershipProvider) -> None:
        # Arrange
        fresh = await provider.try_update_silo(_silo())
        fresh_with_change = SiloInfo(
            address=fresh.address,
            status=SiloStatus.DEAD,
            last_heartbeat=fresh.last_heartbeat,
            start_time=fresh.start_time,
            cluster_id=fresh.cluster_id,
            gateway_port=fresh.gateway_port,
            i_am_alive=fresh.i_am_alive,
            suspicions=list(fresh.suspicions),
            etag="stale",
        )

        # Act / Assert
        with pytest.raises(TableStaleError):
            await provider.try_update_silo(fresh_with_change)


class TestTryAddSuspicion:
    async def test_append_vote(self, provider: MembershipProvider) -> None:
        # Arrange
        silo = await provider.try_update_silo(_silo())
        vote = SuspicionVote(suspecting_silo="10.0.0.2:11111:1700000000", timestamp=time.time())

        # Act
        updated = await provider.try_add_suspicion(
            silo.address.silo_id, vote, expected_etag=silo.etag or ""
        )

        # Assert
        assert len(updated.suspicions) == 1
        assert updated.suspicions[0] == vote

    async def test_stale_etag_raises(self, provider: MembershipProvider) -> None:
        # Arrange
        silo = await provider.try_update_silo(_silo())
        vote = SuspicionVote(suspecting_silo="x:1:1", timestamp=time.time())

        # Act / Assert
        with pytest.raises(TableStaleError):
            await provider.try_add_suspicion(silo.address.silo_id, vote, "stale")

    async def test_unknown_silo_raises(self, provider: MembershipProvider) -> None:
        # Arrange
        vote = SuspicionVote(suspecting_silo="x:1:1", timestamp=time.time())

        # Act / Assert
        with pytest.raises(SiloNotFoundError):
            await provider.try_add_suspicion("nope:0:0", vote, "ignored")

    async def test_concurrent_appenders_serialize(self, provider: MembershipProvider) -> None:
        # Arrange
        silo = await provider.try_update_silo(_silo())
        silo_id = silo.address.silo_id

        async def vote_with_retry(index: int) -> None:
            while True:
                current = await provider.read_silo(silo_id)
                assert current is not None
                vote = SuspicionVote(suspecting_silo=f"suspect-{index}:0:0", timestamp=time.time())
                try:
                    await provider.try_add_suspicion(
                        silo_id, vote, expected_etag=current.etag or ""
                    )
                    return
                except TableStaleError:
                    continue

        # Act
        await asyncio.gather(*(vote_with_retry(i) for i in range(5)))

        # Assert
        final = await provider.read_silo(silo_id)
        assert final is not None
        suspectors = {v.suspecting_silo for v in final.suspicions}
        assert suspectors == {f"suspect-{i}:0:0" for i in range(5)}


class TestPhase2Fields:
    async def test_cluster_id_and_gateway_port_persist(self, provider: MembershipProvider) -> None:
        # Arrange
        silo = _silo()

        # Act
        await provider.try_update_silo(silo)

        # Assert
        refreshed = await provider.read_silo(silo.address.silo_id)
        assert refreshed is not None
        assert refreshed.cluster_id == "demo"
        assert refreshed.gateway_port == 30000

    async def test_i_am_alive_round_trips(self, provider: MembershipProvider) -> None:
        # Arrange
        silo = _silo()
        silo.i_am_alive = 1_700_000_123.5

        # Act
        await provider.try_update_silo(silo)

        # Assert
        refreshed = await provider.read_silo(silo.address.silo_id)
        assert refreshed is not None
        assert refreshed.i_am_alive == pytest.approx(1_700_000_123.5)


class TestProviderParametrisationCovered:
    def test_both_providers_present(self, provider: MembershipProvider) -> None:
        # Assert
        assert isinstance(provider, YamlMembershipProvider | MarkdownTableMembershipProvider)


def _unused() -> Any:  # pragma: no cover
    return None
