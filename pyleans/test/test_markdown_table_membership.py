"""Tests for Markdown-table membership provider."""

import time
from pathlib import Path

import pytest
from pyleans.errors import MembershipError
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus
from pyleans.server.providers.markdown_table_membership import (
    MarkdownTableMembershipProvider,
)


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
def provider(tmp_path: Path) -> MarkdownTableMembershipProvider:
    return MarkdownTableMembershipProvider(file_path=str(tmp_path / "membership.md"))


class TestRegisterSilo:
    async def test_register_creates_file(self, provider: MarkdownTableMembershipProvider) -> None:
        await provider.register_silo(make_silo())
        assert provider._file_path.exists()

    async def test_register_and_get(self, provider: MarkdownTableMembershipProvider) -> None:
        silo = make_silo()
        await provider.register_silo(silo)
        silos = await provider.get_active_silos()
        assert len(silos) == 1
        assert silos[0].address == silo.address

    async def test_register_multiple_silos(self, provider: MarkdownTableMembershipProvider) -> None:
        await provider.register_silo(make_silo(port=11111, epoch=1000))
        await provider.register_silo(make_silo(port=11112, epoch=2000))
        silos = await provider.get_active_silos()
        assert len(silos) == 2

    async def test_register_updates_existing(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        silo = make_silo()
        await provider.register_silo(silo)
        silo.status = SiloStatus.SHUTTING_DOWN
        await provider.register_silo(silo)

        state = provider._read_file()
        assert len(state.silos) == 1
        assert state.silos[0].status == SiloStatus.SHUTTING_DOWN


class TestUnregisterSilo:
    async def test_unregister_removes(self, provider: MarkdownTableMembershipProvider) -> None:
        silo = make_silo()
        await provider.register_silo(silo)
        await provider.unregister_silo(silo.address.encoded)
        assert await provider.get_active_silos() == []

    async def test_unregister_nonexistent_is_noop(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        await provider.unregister_silo("nonexistent")


class TestGetActiveSilos:
    async def test_filters_by_status(self, provider: MarkdownTableMembershipProvider) -> None:
        await provider.register_silo(make_silo(port=11111, epoch=1000, status=SiloStatus.ACTIVE))
        await provider.register_silo(make_silo(port=11112, epoch=2000, status=SiloStatus.DEAD))
        await provider.register_silo(make_silo(port=11113, epoch=3000, status=SiloStatus.JOINING))
        silos = await provider.get_active_silos()
        assert len(silos) == 1
        assert silos[0].address.port == 11111

    async def test_empty_returns_empty(self, provider: MarkdownTableMembershipProvider) -> None:
        assert await provider.get_active_silos() == []


class TestHeartbeat:
    async def test_heartbeat_updates_timestamp(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        silo = make_silo()
        silo.last_heartbeat = 1000.0
        await provider.register_silo(silo)

        await provider.heartbeat(silo.address.encoded)
        state = provider._read_file()
        assert state.silos[0].last_heartbeat > 1000.0

    async def test_heartbeat_unknown_silo_raises(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        with pytest.raises(MembershipError):
            await provider.heartbeat("nonexistent")


class TestUpdateStatus:
    async def test_update_status(self, provider: MarkdownTableMembershipProvider) -> None:
        silo = make_silo(status=SiloStatus.JOINING)
        await provider.register_silo(silo)
        await provider.update_status(silo.address.encoded, SiloStatus.ACTIVE)

        silos = await provider.get_active_silos()
        assert len(silos) == 1
        assert silos[0].status == SiloStatus.ACTIVE

    async def test_update_unknown_silo_raises(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        with pytest.raises(MembershipError):
            await provider.update_status("nonexistent", SiloStatus.DEAD)


class TestVersioning:
    async def test_version_increments(self, provider: MarkdownTableMembershipProvider) -> None:
        silo = make_silo()
        await provider.register_silo(silo)
        v1 = provider._read_file().version
        await provider.heartbeat(silo.address.encoded)
        v2 = provider._read_file().version
        assert v2 > v1


class TestMarkdownShape:
    async def test_file_contains_table_header(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        await provider.register_silo(make_silo())
        content = provider._file_path.read_text(encoding="utf-8")
        assert "# Membership" in content
        assert "| ID | Host | Port | Epoch | Status | Last Heartbeat | Start Time |" in content
        assert "| --- |" in content

    async def test_file_contains_row_per_silo(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        await provider.register_silo(make_silo(host="a", port=1, epoch=1))
        await provider.register_silo(make_silo(host="b", port=2, epoch=2))
        content = provider._file_path.read_text(encoding="utf-8")
        assert "| a_1_1 |" in content
        assert "| b_2_2 |" in content

    async def test_malformed_row_raises(
        self, provider: MarkdownTableMembershipProvider, tmp_path: Path
    ) -> None:
        path = tmp_path / "bad.md"
        path.write_text(
            "# Membership\n\nVersion: 1\n\n"
            "| ID | Host | Port | Epoch | Status | Last Heartbeat | Start Time |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| too | few | cells |\n",
            encoding="utf-8",
        )
        bad = MarkdownTableMembershipProvider(file_path=str(path))
        with pytest.raises(MembershipError):
            await bad.get_active_silos()

    async def test_pipe_in_host_is_rejected(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        silo = make_silo(host="bad|host")
        with pytest.raises(MembershipError):
            await provider.register_silo(silo)

    async def test_newline_in_host_is_rejected(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        silo = make_silo(host="bad\nhost")
        with pytest.raises(MembershipError):
            await provider.register_silo(silo)

    async def test_parsing_stops_at_blank_line(
        self, provider: MarkdownTableMembershipProvider, tmp_path: Path
    ) -> None:
        path = tmp_path / "with_extra.md"
        path.write_text(
            "# Membership\n\nVersion: 1\n\n"
            "| ID | Host | Port | Epoch | Status | Last Heartbeat | Start Time |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "\n"
            "# Notes\n\n"
            "| a | b |\n",
            encoding="utf-8",
        )
        extra = MarkdownTableMembershipProvider(file_path=str(path))
        assert await extra.get_active_silos() == []
