"""Tests for pyleans.cluster.hash_ring — ConsistentHashRing."""

import random
import subprocess
import sys

import pytest
from pyleans.cluster.hash_ring import (
    VIRTUAL_NODES_PER_SILO,
    ConsistentHashRing,
    RingPosition,
)
from pyleans.cluster.identity import hash_grain_id
from pyleans.identity import GrainId, SiloAddress


def _silo(host: str, port: int = 11111, epoch: int = 1_700_000_000) -> SiloAddress:
    return SiloAddress(host=host, port=port, epoch=epoch)


class TestConstruction:
    def test_empty_silo_list_produces_empty_ring(self) -> None:
        # Act
        ring = ConsistentHashRing([])

        # Assert
        assert len(ring) == 0
        assert ring.silos == []
        assert ring.positions == []

    def test_single_silo_creates_vnodes_positions(self) -> None:
        # Arrange
        silo = _silo("10.0.0.1")

        # Act
        ring = ConsistentHashRing([silo])

        # Assert
        assert len(ring) == 1
        assert ring.silos == [silo]
        assert len(ring.positions) == VIRTUAL_NODES_PER_SILO

    def test_three_silos_create_correct_total_positions(self) -> None:
        # Arrange
        silos = [_silo("10.0.0.1"), _silo("10.0.0.2"), _silo("10.0.0.3")]

        # Act
        ring = ConsistentHashRing(silos)

        # Assert
        assert len(ring) == 3
        assert len(ring.positions) == 3 * VIRTUAL_NODES_PER_SILO

    def test_positions_sorted_by_hash(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 6)]

        # Act
        ring = ConsistentHashRing(silos)

        # Assert
        hashes = [p.position for p in ring.positions]
        assert hashes == sorted(hashes)

    def test_silos_deduplicated(self) -> None:
        # Arrange
        silo = _silo("10.0.0.1")

        # Act
        ring = ConsistentHashRing([silo, silo, silo])

        # Assert
        assert len(ring) == 1
        assert len(ring.positions) == VIRTUAL_NODES_PER_SILO

    def test_silos_snapshot_sorted_by_silo_id(self) -> None:
        # Arrange
        silos = [_silo("10.0.0.3"), _silo("10.0.0.1"), _silo("10.0.0.2")]

        # Act
        ring = ConsistentHashRing(silos)

        # Assert
        assert [s.silo_id for s in ring.silos] == sorted(s.silo_id for s in silos)

    def test_custom_vnode_count(self) -> None:
        # Arrange
        silos = [_silo("10.0.0.1"), _silo("10.0.0.2")]

        # Act
        ring = ConsistentHashRing(silos, virtual_nodes_per_silo=5)

        # Assert
        assert len(ring.positions) == 10

    def test_zero_vnodes_raises(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            ConsistentHashRing([_silo("10.0.0.1")], virtual_nodes_per_silo=0)

    def test_negative_vnodes_raises(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            ConsistentHashRing([_silo("10.0.0.1")], virtual_nodes_per_silo=-1)


class TestOwnerOf:
    def test_empty_ring_returns_none(self) -> None:
        # Arrange
        ring = ConsistentHashRing([])

        # Act
        result = ring.owner_of(hash_grain_id(GrainId("CounterGrain", "a")))

        # Assert
        assert result is None

    def test_single_silo_owns_any_key(self) -> None:
        # Arrange
        silo = _silo("10.0.0.1")
        ring = ConsistentHashRing([silo])

        # Act
        owners = [ring.owner_of(hash_grain_id(GrainId("CounterGrain", str(i)))) for i in range(50)]

        # Assert
        assert all(o == silo for o in owners)

    def test_returns_silo_at_first_position_gte_key(self) -> None:
        # Arrange
        silos = [_silo("10.0.0.1"), _silo("10.0.0.2"), _silo("10.0.0.3")]
        ring = ConsistentHashRing(silos)
        first_position = ring.positions[0]

        # Act
        result = ring.owner_of(first_position.position)

        # Assert
        assert result == first_position.silo

    def test_key_beyond_max_position_wraps_around(self) -> None:
        # Arrange
        silos = [_silo("10.0.0.1"), _silo("10.0.0.2")]
        ring = ConsistentHashRing(silos)
        max_key = 2**64 - 1

        # Act
        result = ring.owner_of(max_key)

        # Assert
        assert result == ring.positions[0].silo

    def test_owner_is_deterministic(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 5)]
        ring_a = ConsistentHashRing(silos)
        ring_b = ConsistentHashRing(list(reversed(silos)))
        grain_hash = hash_grain_id(GrainId("CounterGrain", "xyz"))

        # Act
        owner_a = ring_a.owner_of(grain_hash)
        owner_b = ring_b.owner_of(grain_hash)

        # Assert
        assert owner_a == owner_b


class TestSuccessors:
    def test_empty_ring_returns_empty_list(self) -> None:
        # Arrange
        ring = ConsistentHashRing([])

        # Act
        result = ring.successors(_silo("10.0.0.1"), 3)

        # Assert
        assert result == []

    def test_single_silo_returns_empty_list(self) -> None:
        # Arrange
        silo = _silo("10.0.0.1")
        ring = ConsistentHashRing([silo])

        # Act
        result = ring.successors(silo, 3)

        # Assert
        assert result == []

    def test_unknown_silo_returns_empty_list(self) -> None:
        # Arrange
        ring = ConsistentHashRing([_silo("10.0.0.1"), _silo("10.0.0.2")])

        # Act
        result = ring.successors(_silo("10.0.0.99"), 3)

        # Assert
        assert result == []

    def test_zero_count_returns_empty_list(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 5)]
        ring = ConsistentHashRing(silos)

        # Act
        result = ring.successors(silos[0], 0)

        # Assert
        assert result == []

    def test_three_successors_distinct(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 6)]
        ring = ConsistentHashRing(silos)

        # Act
        result = ring.successors(silos[0], 3)

        # Assert
        assert len(result) == 3
        assert len(set(result)) == 3
        assert silos[0] not in result

    def test_count_beyond_available_returns_all_others(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 6)]
        ring = ConsistentHashRing(silos)

        # Act
        result = ring.successors(silos[0], 100)

        # Assert
        assert len(result) == 4
        assert set(result) == {s for s in silos if s != silos[0]}

    def test_successors_order_deterministic(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 8)]
        ring_a = ConsistentHashRing(silos)
        ring_b = ConsistentHashRing(list(reversed(silos)))

        # Act
        list_a = ring_a.successors(silos[0], 5)
        list_b = ring_b.successors(silos[0], 5)

        # Assert
        assert list_a == list_b


class TestMembershipAndLen:
    def test_contains_true_for_member(self) -> None:
        # Arrange
        silo = _silo("10.0.0.1")
        ring = ConsistentHashRing([silo, _silo("10.0.0.2")])

        # Act / Assert
        assert silo in ring

    def test_contains_false_for_non_member(self) -> None:
        # Arrange
        ring = ConsistentHashRing([_silo("10.0.0.1")])

        # Act / Assert
        assert _silo("10.0.0.99") not in ring

    def test_contains_false_for_wrong_type(self) -> None:
        # Arrange
        ring = ConsistentHashRing([_silo("10.0.0.1")])

        # Act / Assert
        assert "not-a-silo" not in ring

    def test_len_counts_physical_silos(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 5)]

        # Act
        ring = ConsistentHashRing(silos)

        # Assert
        assert len(ring) == 4


class TestRebuildUnderChurn:
    def test_adding_silo_remaps_approximately_one_over_n(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 5)]
        ring_before = ConsistentHashRing(silos)
        ring_after = ConsistentHashRing([*silos, _silo("10.0.0.99")])
        num_keys = 10_000
        key_hashes = [hash_grain_id(GrainId("CounterGrain", f"key-{i}")) for i in range(num_keys)]

        # Act
        changed = sum(1 for h in key_hashes if ring_before.owner_of(h) != ring_after.owner_of(h))

        # Assert
        fraction = changed / num_keys
        expected = 1 / 5
        assert 0.05 <= fraction <= 0.25, f"expected ~{expected:.2%} remapped, got {fraction:.2%}"

    def test_removing_silo_remaps_only_that_silos_keys(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 5)]
        dropped = silos[0]
        ring_before = ConsistentHashRing(silos)
        ring_after = ConsistentHashRing(silos[1:])
        num_keys = 10_000
        key_hashes = [hash_grain_id(GrainId("CounterGrain", f"key-{i}")) for i in range(num_keys)]

        # Act
        moved_from_dropped = sum(
            1
            for h in key_hashes
            if ring_before.owner_of(h) == dropped and ring_after.owner_of(h) != dropped
        )
        moved_from_kept = sum(
            1
            for h in key_hashes
            if ring_before.owner_of(h) != dropped
            and ring_before.owner_of(h) != ring_after.owner_of(h)
        )

        # Assert
        assert moved_from_kept == 0, "only dropped silo's keys should move"
        assert moved_from_dropped > 0

    def test_successor_exhaustion_under_shrinking_cluster(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 4)]
        ring = ConsistentHashRing(silos)

        # Act
        all_successors = ring.successors(silos[0], 10)

        # Assert
        assert len(all_successors) == 2


class TestLoadBalance:
    def test_arc_share_smoke_bound(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 11)]
        ring = ConsistentHashRing(silos)
        ring_space = 2**64
        expected_share = 1 / len(silos)
        lower_bound = 0.5 * expected_share
        upper_bound = 1.5 * expected_share

        # Act
        arc_share: dict[SiloAddress, int] = {s: 0 for s in silos}
        positions = ring.positions
        for index, current in enumerate(positions):
            next_position = positions[(index + 1) % len(positions)]
            arc = (next_position.position - current.position) % ring_space
            arc_share[next_position.silo] += arc

        # Assert
        for silo, arc in arc_share.items():
            share = arc / ring_space
            assert lower_bound <= share <= upper_bound, (
                f"silo {silo.silo_id} owns {share:.4f} share, expected "
                f"[{lower_bound:.4f}, {upper_bound:.4f}]"
            )


class TestDeterminism:
    def test_shuffled_input_produces_identical_ring(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 11)]
        shuffled = list(silos)
        random.Random(42).shuffle(shuffled)

        # Act
        ring_a = ConsistentHashRing(silos)
        ring_b = ConsistentHashRing(shuffled)

        # Assert
        assert [p.position for p in ring_a.positions] == [p.position for p in ring_b.positions]
        assert [p.silo for p in ring_a.positions] == [p.silo for p in ring_b.positions]

    def test_cross_process_deterministic(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 5)]
        grain_keys = [f"grain-{i}" for i in range(10)]
        ring = ConsistentHashRing(silos)
        expected = [
            (
                key,
                ring.owner_of(hash_grain_id(GrainId("CounterGrain", key))).silo_id,  # type: ignore[union-attr]
            )
            for key in grain_keys
        ]
        script = (
            "from pyleans.cluster.hash_ring import ConsistentHashRing\n"
            "from pyleans.cluster.identity import hash_grain_id\n"
            "from pyleans.identity import GrainId, SiloAddress\n"
            "silos = [SiloAddress(host=f'10.0.0.{i}', port=11111, epoch=1700000000) "
            "for i in range(1, 5)]\n"
            "ring = ConsistentHashRing(silos)\n"
            "for key in " + repr(grain_keys) + ":\n"
            "    owner = ring.owner_of(hash_grain_id(GrainId('CounterGrain', key)))\n"
            "    assert owner is not None\n"
            "    print(f'{key}:{owner.silo_id}')\n"
        )

        # Act
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": "random", "PATH": ""},
        )
        child_output = [tuple(line.split(":", 1)) for line in completed.stdout.strip().splitlines()]

        # Assert
        assert child_output == expected


class TestRingPosition:
    def test_ring_position_is_frozen(self) -> None:
        # Arrange
        pos = RingPosition(position=1, silo=_silo("10.0.0.1"), vnode_index=0)

        # Act / Assert
        with pytest.raises(AttributeError):
            pos.position = 2  # type: ignore[misc]
