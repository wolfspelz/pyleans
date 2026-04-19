"""Tests for pyleans.cluster — ClusterId and deterministic hashing."""

import subprocess
import sys

import pytest
from pyleans.cluster import (
    ClusterId,
    hash_grain_id,
    hash_silo_virtual_node,
    stable_hash,
)
from pyleans.identity import GrainId, SiloAddress


class TestClusterId:
    def test_creation_returns_object_with_value(self) -> None:
        # Act
        cid = ClusterId("dev-cluster")

        # Assert
        assert cid.value == "dev-cluster"

    def test_empty_string_raises_value_error(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            ClusterId("")

    def test_slash_in_value_raises_value_error(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            ClusterId("bad/id")

    def test_null_byte_in_value_raises_value_error(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            ClusterId("bad\0id")

    def test_frozen_prevents_mutation(self) -> None:
        # Arrange
        cid = ClusterId("dev-cluster")

        # Act / Assert
        with pytest.raises(AttributeError):
            cid.value = "changed"  # type: ignore[misc]

    def test_equality_same_value(self) -> None:
        # Act
        result = ClusterId("dev-cluster") == ClusterId("dev-cluster")

        # Assert
        assert result is True

    def test_inequality_different_value(self) -> None:
        # Act
        result = ClusterId("dev-cluster") == ClusterId("prod-cluster")

        # Assert
        assert result is False

    def test_hashable_and_usable_as_dict_key(self) -> None:
        # Arrange
        cid = ClusterId("dev-cluster")
        d: dict[ClusterId, str] = {cid: "payload"}

        # Act
        result = d[ClusterId("dev-cluster")]

        # Assert
        assert result == "payload"

    def test_str_returns_value(self) -> None:
        # Act
        result = str(ClusterId("dev-cluster"))

        # Assert
        assert result == "dev-cluster"


class TestStableHash:
    def test_returns_int(self) -> None:
        # Act
        result = stable_hash(b"abc")

        # Assert
        assert isinstance(result, int)

    def test_returns_non_negative(self) -> None:
        # Act
        result = stable_hash(b"abc")

        # Assert
        assert result >= 0

    def test_returns_value_within_64_bits(self) -> None:
        # Act
        result = stable_hash(b"abc")

        # Assert
        assert result < 2**64

    def test_deterministic_within_process(self) -> None:
        # Act
        a = stable_hash(b"abc")
        b = stable_hash(b"abc")

        # Assert
        assert a == b

    def test_distinct_inputs_produce_distinct_outputs(self) -> None:
        # Act
        a = stable_hash(b"abc")
        b = stable_hash(b"xyz")

        # Assert
        assert a != b

    def test_empty_bytes(self) -> None:
        # Act
        result = stable_hash(b"")

        # Assert
        assert isinstance(result, int)
        assert 0 <= result < 2**64

    def test_cross_process_deterministic(self) -> None:
        # Arrange
        expected = stable_hash(b"hello-cluster")
        script = "from pyleans.cluster import stable_hash; print(stable_hash(b'hello-cluster'))"

        # Act
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": "random", "PATH": ""},
        )

        # Assert
        assert int(completed.stdout.strip()) == expected

    def test_distribution_smoke_check(self) -> None:
        # Arrange
        num_keys = 10_000
        num_buckets = 16

        # Act
        buckets = [0] * num_buckets
        for i in range(num_keys):
            buckets[stable_hash(f"grain-{i}".encode()) % num_buckets] += 1

        # Assert
        expected = num_keys / num_buckets
        for count in buckets:
            assert 0.75 * expected < count < 1.25 * expected, buckets


class TestHashGrainId:
    def test_returns_non_negative_64_bit(self) -> None:
        # Act
        result = hash_grain_id(GrainId("CounterGrain", "a"))

        # Assert
        assert 0 <= result < 2**64

    def test_same_grain_id_hashes_to_same_value(self) -> None:
        # Act
        a = hash_grain_id(GrainId("CounterGrain", "a"))
        b = hash_grain_id(GrainId("CounterGrain", "a"))

        # Assert
        assert a == b

    def test_different_grain_type_hashes_differently(self) -> None:
        # Act
        a = hash_grain_id(GrainId("CounterGrain", "a"))
        b = hash_grain_id(GrainId("PlayerGrain", "a"))

        # Assert
        assert a != b

    def test_different_key_hashes_differently(self) -> None:
        # Act
        a = hash_grain_id(GrainId("CounterGrain", "a"))
        b = hash_grain_id(GrainId("CounterGrain", "b"))

        # Assert
        assert a != b

    def test_matches_canonical_string_form(self) -> None:
        # Act
        result = hash_grain_id(GrainId("CounterGrain", "a"))

        # Assert
        assert result == stable_hash(b"CounterGrain/a")


class TestHashSiloVirtualNode:
    def test_returns_non_negative_64_bit(self) -> None:
        # Arrange
        silo = SiloAddress(host="10.0.0.1", port=11111, epoch=1700000000)

        # Act
        result = hash_silo_virtual_node(silo, 0)

        # Assert
        assert 0 <= result < 2**64

    def test_deterministic_for_same_silo_and_index(self) -> None:
        # Arrange
        silo = SiloAddress(host="10.0.0.1", port=11111, epoch=1700000000)

        # Act
        a = hash_silo_virtual_node(silo, 7)
        b = hash_silo_virtual_node(silo, 7)

        # Assert
        assert a == b

    def test_different_vnode_indices_hash_differently(self) -> None:
        # Arrange
        silo = SiloAddress(host="10.0.0.1", port=11111, epoch=1700000000)

        # Act
        a = hash_silo_virtual_node(silo, 0)
        b = hash_silo_virtual_node(silo, 1)

        # Assert
        assert a != b

    def test_different_silos_hash_differently_for_same_vnode(self) -> None:
        # Arrange
        a_silo = SiloAddress(host="10.0.0.1", port=11111, epoch=1700000000)
        b_silo = SiloAddress(host="10.0.0.2", port=11111, epoch=1700000000)

        # Act
        a = hash_silo_virtual_node(a_silo, 0)
        b = hash_silo_virtual_node(b_silo, 0)

        # Assert
        assert a != b

    def test_matches_canonical_string_form(self) -> None:
        # Arrange
        silo = SiloAddress(host="10.0.0.1", port=11111, epoch=1700000000)

        # Act
        result = hash_silo_virtual_node(silo, 3)

        # Assert
        assert result == stable_hash(b"10.0.0.1:11111:1700000000#3")
