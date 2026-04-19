"""Tests for the Markdown-table membership provider file format.

OCC-contract coverage lives in :mod:`test_membership_occ`. The tests in
this module focus on the Markdown file shape (header, rows, malformed
input handling, cell-safety) specific to this provider.
"""

from __future__ import annotations

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
    now = time.time()
    return SiloInfo(
        address=SiloAddress(host=host, port=port, epoch=epoch),
        status=status,
        last_heartbeat=now,
        start_time=now,
        i_am_alive=now,
    )


@pytest.fixture
def provider(tmp_path: Path) -> MarkdownTableMembershipProvider:
    return MarkdownTableMembershipProvider(file_path=str(tmp_path / "membership.md"))


class TestFileFormat:
    async def test_creates_file_on_first_write(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        # Act
        await provider.try_update_silo(make_silo())

        # Assert
        assert provider._file_path.exists()

    async def test_file_contains_markdown_header(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        # Arrange
        await provider.try_update_silo(make_silo())

        # Act
        content = provider._file_path.read_text(encoding="utf-8")

        # Assert
        assert "# Membership" in content
        assert "ID | Host | Port | Epoch | Status" in content
        assert "| --- |" in content

    async def test_file_contains_row_per_silo(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        # Arrange
        await provider.try_update_silo(make_silo(host="a", port=1, epoch=1))
        await provider.try_update_silo(make_silo(host="b", port=2, epoch=2))

        # Act
        content = provider._file_path.read_text(encoding="utf-8")

        # Assert
        assert "| a_1_1 |" in content
        assert "| b_2_2 |" in content


class TestCellSafety:
    async def test_pipe_in_host_is_rejected(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        # Arrange
        silo = make_silo(host="bad|host")

        # Act / Assert
        with pytest.raises(MembershipError):
            await provider.try_update_silo(silo)

    async def test_newline_in_host_is_rejected(
        self, provider: MarkdownTableMembershipProvider
    ) -> None:
        # Arrange
        silo = make_silo(host="bad\nhost")

        # Act / Assert
        with pytest.raises(MembershipError):
            await provider.try_update_silo(silo)


class TestMalformedInput:
    async def test_malformed_row_raises(
        self, provider: MarkdownTableMembershipProvider, tmp_path: Path
    ) -> None:
        # Arrange — replace a valid row with a short one
        await provider.try_update_silo(make_silo(host="a", port=1, epoch=1))
        content = provider._file_path.read_text(encoding="utf-8")
        broken = tmp_path / "broken.md"
        broken.write_text(
            content.replace(
                content.splitlines()[-2],
                "| too | few | cells |",
            ),
            encoding="utf-8",
        )
        bad = MarkdownTableMembershipProvider(file_path=str(broken))

        # Act / Assert
        with pytest.raises(MembershipError):
            await bad.read_all()

    async def test_parsing_stops_at_blank_line(
        self, provider: MarkdownTableMembershipProvider, tmp_path: Path
    ) -> None:
        # Arrange — write a file with the current-format header then blank line + extra noise
        await provider.try_update_silo(make_silo())
        content = provider._file_path.read_text(encoding="utf-8")
        header = next(line for line in content.splitlines() if line.startswith("| ID |"))
        separator = next(line for line in content.splitlines() if line.startswith("| --- |"))
        noisy = tmp_path / "with_extra.md"
        noisy.write_text(
            f"# Membership\n\nVersion: 1\n\n{header}\n{separator}\n\n# Notes\n\n| a | b |\n",
            encoding="utf-8",
        )
        extra = MarkdownTableMembershipProvider(file_path=str(noisy))

        # Act
        snapshot = await extra.read_all()

        # Assert
        assert snapshot.silos == []
