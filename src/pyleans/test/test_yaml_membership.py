"""Tests for the YAML membership provider file format and lifecycle.

OCC-contract coverage lives in :mod:`test_membership_occ`. The tests in
this module focus on file-format particulars (YAML structure, version
bump on write, read-after-write) that the OCC suite does not need to
exercise per-provider.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus
from pyleans.server.providers.yaml_membership import YamlMembershipProvider


def make_silo(
    host: str = "localhost",
    port: int = 11111,
    epoch: int = 1000,
    status: SiloStatus = SiloStatus.ACTIVE,
) -> SiloInfo:
    now = time.time()
    return SiloInfo(
        address=SiloAddress(host=host, port=port, epoch=epoch),
        status=status,
        last_heartbeat=now,
        start_time=now,
        i_am_alive=now,
    )


@pytest.fixture
def provider(tmp_path: Path) -> YamlMembershipProvider:
    return YamlMembershipProvider(file_path=str(tmp_path / "membership.yaml"))


class TestFileFormat:
    async def test_creates_file_with_version_and_silos_keys(
        self, provider: YamlMembershipProvider
    ) -> None:
        # Arrange
        silo = make_silo()

        # Act
        await provider.try_update_silo(silo)

        # Assert
        data = yaml.safe_load(provider._file_path.read_text(encoding="utf-8"))
        assert "version" in data
        assert "silos" in data
        assert len(data["silos"]) == 1

    async def test_row_stores_new_phase2_fields(self, provider: YamlMembershipProvider) -> None:
        # Arrange
        silo = make_silo()
        silo.cluster_id = "demo"
        silo.gateway_port = 30000

        # Act
        await provider.try_update_silo(silo)

        # Assert
        data = yaml.safe_load(provider._file_path.read_text(encoding="utf-8"))
        row = data["silos"][0]
        assert row["cluster_id"] == "demo"
        assert row["gateway_port"] == 30000
        assert "i_am_alive" in row
        assert "suspicions" in row
        assert row["etag"] is not None

    async def test_multiple_silos_round_trip(self, provider: YamlMembershipProvider) -> None:
        # Arrange
        a = make_silo(port=11111, epoch=1000)
        b = make_silo(port=11112, epoch=2000)

        # Act
        await provider.try_update_silo(a)
        await provider.try_update_silo(b)
        snapshot = await provider.read_all()

        # Assert
        assert len(snapshot.silos) == 2


class TestGetActive:
    async def test_filters_by_status(self, provider: YamlMembershipProvider) -> None:
        # Arrange
        active = make_silo(port=11111, epoch=1000, status=SiloStatus.ACTIVE)
        dead = make_silo(port=11112, epoch=2000, status=SiloStatus.DEAD)

        # Act
        await provider.try_update_silo(active)
        await provider.try_update_silo(dead)
        snapshot = await provider.read_all()
        active_rows = [s for s in snapshot.silos if s.status == SiloStatus.ACTIVE]

        # Assert
        assert len(active_rows) == 1
        assert active_rows[0].address.port == 11111

    async def test_empty_snapshot_has_no_silos(self, provider: YamlMembershipProvider) -> None:
        # Act
        snapshot = await provider.read_all()

        # Assert
        assert snapshot.silos == []


class TestVersionIncrement:
    async def test_version_bumps_on_every_write(self, provider: YamlMembershipProvider) -> None:
        # Arrange
        silo = await provider.try_update_silo(make_silo())
        first_version = (await provider.read_all()).version

        # Act
        silo.status = SiloStatus.SHUTTING_DOWN
        await provider.try_update_silo(silo)
        second_version = (await provider.read_all()).version

        # Assert
        assert second_version == first_version + 1


class TestPhysicalDelete:
    async def test_try_delete_silo_removes_row(self, provider: YamlMembershipProvider) -> None:
        # Arrange
        silo = await provider.try_update_silo(make_silo())

        # Act
        await provider.try_delete_silo(silo)

        # Assert
        snapshot = await provider.read_all()
        assert snapshot.silos == []


class TestHumanReadableYaml:
    async def test_file_is_valid_yaml(self, provider: YamlMembershipProvider) -> None:
        # Arrange
        await provider.try_update_silo(make_silo())

        # Act
        content = provider._file_path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)

        # Assert
        assert isinstance(data, dict)
        assert "silos" in data
