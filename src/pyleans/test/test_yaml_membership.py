"""Tests for YAML membership provider."""

import time

import pytest
import yaml
from pyleans.errors import MembershipError
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus
from pyleans.server.providers.yaml_membership import YamlMembershipProvider


def make_silo(
    host: str = "localhost",
    port: int = 11111,
    epoch: int = 1000,
    status: SiloStatus = SiloStatus.ACTIVE,
) -> SiloInfo:
    return SiloInfo(
        address=SiloAddress(host=host, port=port, epoch=epoch),
        status=status,
        last_heartbeat=time.time(),
        start_time=time.time(),
    )


@pytest.fixture
def provider(tmp_path: object) -> YamlMembershipProvider:
    return YamlMembershipProvider(file_path=str(tmp_path) + "/membership.yaml")


class TestRegisterSilo:
    async def test_register_creates_file(self, provider: YamlMembershipProvider) -> None:
        silo = make_silo()
        await provider.register_silo(silo)
        assert provider._file_path.exists()

    async def test_register_and_get(self, provider: YamlMembershipProvider) -> None:
        silo = make_silo()
        await provider.register_silo(silo)
        silos = await provider.get_active_silos()
        assert len(silos) == 1
        assert silos[0].address == silo.address

    async def test_register_multiple_silos(self, provider: YamlMembershipProvider) -> None:
        s1 = make_silo(port=11111, epoch=1000)
        s2 = make_silo(port=11112, epoch=2000)
        await provider.register_silo(s1)
        await provider.register_silo(s2)
        silos = await provider.get_active_silos()
        assert len(silos) == 2

    async def test_register_updates_existing(self, provider: YamlMembershipProvider) -> None:
        silo = make_silo()
        await provider.register_silo(silo)
        silo.status = SiloStatus.SHUTTING_DOWN
        await provider.register_silo(silo)

        data = provider._read_file()
        assert len(data["silos"]) == 1
        assert data["silos"][0]["status"] == "shutting_down"


class TestUnregisterSilo:
    async def test_unregister_removes(self, provider: YamlMembershipProvider) -> None:
        silo = make_silo()
        await provider.register_silo(silo)
        await provider.unregister_silo(silo.address.encoded)
        silos = await provider.get_active_silos()
        assert len(silos) == 0

    async def test_unregister_nonexistent_is_noop(self, provider: YamlMembershipProvider) -> None:
        await provider.unregister_silo("nonexistent")


class TestGetActiveSilos:
    async def test_filters_by_status(self, provider: YamlMembershipProvider) -> None:
        active = make_silo(port=11111, epoch=1000, status=SiloStatus.ACTIVE)
        dead = make_silo(port=11112, epoch=2000, status=SiloStatus.DEAD)
        joining = make_silo(port=11113, epoch=3000, status=SiloStatus.JOINING)
        await provider.register_silo(active)
        await provider.register_silo(dead)
        await provider.register_silo(joining)

        silos = await provider.get_active_silos()
        assert len(silos) == 1
        assert silos[0].address.port == 11111

    async def test_empty_returns_empty(self, provider: YamlMembershipProvider) -> None:
        silos = await provider.get_active_silos()
        assert silos == []


class TestHeartbeat:
    async def test_heartbeat_updates_timestamp(self, provider: YamlMembershipProvider) -> None:
        silo = make_silo()
        silo.last_heartbeat = 1000.0
        await provider.register_silo(silo)

        await provider.heartbeat(silo.address.encoded)
        data = provider._read_file()
        assert data["silos"][0]["last_heartbeat"] > 1000.0

    async def test_heartbeat_unknown_silo_raises(self, provider: YamlMembershipProvider) -> None:
        with pytest.raises(MembershipError):
            await provider.heartbeat("nonexistent")


class TestUpdateStatus:
    async def test_update_status(self, provider: YamlMembershipProvider) -> None:
        silo = make_silo(status=SiloStatus.JOINING)
        await provider.register_silo(silo)
        await provider.update_status(silo.address.encoded, SiloStatus.ACTIVE)

        silos = await provider.get_active_silos()
        assert len(silos) == 1
        assert silos[0].status == SiloStatus.ACTIVE

    async def test_update_unknown_silo_raises(self, provider: YamlMembershipProvider) -> None:
        with pytest.raises(MembershipError):
            await provider.update_status("nonexistent", SiloStatus.DEAD)


class TestVersioning:
    async def test_version_increments(self, provider: YamlMembershipProvider) -> None:
        silo = make_silo()
        await provider.register_silo(silo)
        data1 = provider._read_file()
        v1 = data1["version"]

        await provider.heartbeat(silo.address.encoded)
        data2 = provider._read_file()
        v2 = data2["version"]
        assert v2 > v1


class TestYamlReadability:
    async def test_file_is_valid_yaml(self, provider: YamlMembershipProvider) -> None:
        silo = make_silo()
        await provider.register_silo(silo)

        content = provider._file_path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        assert "version" in data
        assert "silos" in data
        assert len(data["silos"]) == 1
