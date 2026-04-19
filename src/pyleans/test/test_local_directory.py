"""Tests for :mod:`pyleans.server.local_directory`.

Covers the :class:`IGrainDirectory` contract: register-if-absent
idempotency, safe unregister (no cross-silo eviction),
``resolve_or_activate`` find-or-allocate, and lookup.
"""

from __future__ import annotations

import pytest
from pyleans.cluster.directory import DirectoryEntry
from pyleans.cluster.placement import PreferLocalPlacement, RandomPlacement
from pyleans.identity import GrainId, SiloAddress
from pyleans.server.local_directory import LocalGrainDirectory


def _local() -> SiloAddress:
    return SiloAddress(host="127.0.0.1", port=11111, epoch=1)


def _other() -> SiloAddress:
    return SiloAddress(host="10.0.0.9", port=11111, epoch=1)


def _grain() -> GrainId:
    return GrainId(grain_type="CounterGrain", key="c1")


class TestRegister:
    async def test_register_new_grain_creates_entry(self) -> None:
        # Arrange
        directory = LocalGrainDirectory(_local())

        # Act
        entry = await directory.register(_grain(), _local())

        # Assert
        assert entry.grain_id == _grain()
        assert entry.silo == _local()
        assert entry.activation_epoch == 1

    async def test_register_is_idempotent(self) -> None:
        # Arrange
        directory = LocalGrainDirectory(_local())
        first = await directory.register(_grain(), _local())

        # Act
        second = await directory.register(_grain(), _local())

        # Assert - same entry, same epoch (no increment)
        assert second == first

    async def test_register_with_different_caller_returns_existing(self) -> None:
        # Arrange - local silo owns the grain; another caller's preferred silo is ignored
        directory = LocalGrainDirectory(_local())
        first = await directory.register(_grain(), _local())

        # Act
        second = await directory.register(_grain(), _other())

        # Assert - first writer wins
        assert second == first
        assert second.silo == _local()

    async def test_sequential_registrations_increment_epoch(self) -> None:
        # Arrange
        directory = LocalGrainDirectory(_local())
        g1 = GrainId(grain_type="T", key="k1")
        g2 = GrainId(grain_type="T", key="k2")

        # Act
        e1 = await directory.register(g1, _local())
        e2 = await directory.register(g2, _local())

        # Assert
        assert e1.activation_epoch == 1
        assert e2.activation_epoch == 2


class TestLookup:
    async def test_lookup_registered_grain(self) -> None:
        # Arrange
        directory = LocalGrainDirectory(_local())
        entry = await directory.register(_grain(), _local())

        # Act
        found = await directory.lookup(_grain())

        # Assert
        assert found == entry

    async def test_lookup_missing_grain_returns_none(self) -> None:
        # Arrange
        directory = LocalGrainDirectory(_local())

        # Act
        found = await directory.lookup(_grain())

        # Assert
        assert found is None


class TestUnregister:
    async def test_unregister_removes_entry_when_owner_matches(self) -> None:
        # Arrange
        directory = LocalGrainDirectory(_local())
        await directory.register(_grain(), _local())

        # Act
        await directory.unregister(_grain(), _local())

        # Assert
        assert await directory.lookup(_grain()) is None

    async def test_unregister_other_silo_is_noop(self) -> None:
        # Arrange - entry owned by local; an unregister claim from _other() must not evict
        directory = LocalGrainDirectory(_local())
        entry = await directory.register(_grain(), _local())

        # Act
        await directory.unregister(_grain(), _other())

        # Assert
        assert await directory.lookup(_grain()) == entry

    async def test_unregister_missing_grain_is_noop(self) -> None:
        # Arrange
        directory = LocalGrainDirectory(_local())

        # Act (must not raise)
        await directory.unregister(_grain(), _local())

        # Assert
        assert await directory.lookup(_grain()) is None


class TestResolveOrActivate:
    async def test_resolve_existing_returns_entry_without_reregister(self) -> None:
        # Arrange
        directory = LocalGrainDirectory(_local())
        first = await directory.register(_grain(), _local())

        # Act
        resolved = await directory.resolve_or_activate(_grain(), PreferLocalPlacement(), _local())

        # Assert - same entry, no new epoch
        assert resolved == first

    async def test_resolve_missing_grain_registers_locally(self) -> None:
        # Arrange
        directory = LocalGrainDirectory(_local())

        # Act
        entry = await directory.resolve_or_activate(
            _grain(), PreferLocalPlacement(), caller=_local()
        )

        # Assert
        assert entry.silo == _local()
        assert await directory.lookup(_grain()) == entry

    async def test_resolve_with_random_placement_still_self_assigns(self) -> None:
        # Arrange
        directory = LocalGrainDirectory(_local())

        # Act - RandomPlacement restricted to [_local()] → deterministic result
        entry = await directory.resolve_or_activate(_grain(), RandomPlacement(), caller=None)

        # Assert
        assert entry.silo == _local()


class TestDirectoryEntry:
    def test_entry_fields(self) -> None:
        # Arrange / Act
        entry = DirectoryEntry(grain_id=_grain(), silo=_local(), activation_epoch=5)

        # Assert
        assert entry.grain_id == _grain()
        assert entry.silo == _local()
        assert entry.activation_epoch == 5

    def test_entry_is_frozen(self) -> None:
        # Arrange
        entry = DirectoryEntry(grain_id=_grain(), silo=_local(), activation_epoch=1)

        # Act / Assert
        with pytest.raises(AttributeError):
            entry.activation_epoch = 99  # type: ignore[misc]
