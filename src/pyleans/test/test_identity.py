"""Tests for pyleans.identity — core identity types."""

from pyleans.identity import GrainId, SiloAddress, SiloInfo, SiloStatus


class TestGrainId:
    def test_creation(self) -> None:
        gid = GrainId(grain_type="Counter", key="abc")
        assert gid.grain_type == "Counter"
        assert gid.key == "abc"

    def test_equality_same_values(self) -> None:
        a = GrainId(grain_type="Counter", key="1")
        b = GrainId(grain_type="Counter", key="1")
        assert a == b

    def test_equality_different_type(self) -> None:
        a = GrainId(grain_type="Counter", key="1")
        b = GrainId(grain_type="Player", key="1")
        assert a != b

    def test_equality_different_key(self) -> None:
        a = GrainId(grain_type="Counter", key="1")
        b = GrainId(grain_type="Counter", key="2")
        assert a != b

    def test_hashable_and_dict_key(self) -> None:
        gid = GrainId(grain_type="Counter", key="1")
        d: dict[GrainId, str] = {gid: "value"}
        assert d[GrainId(grain_type="Counter", key="1")] == "value"

    def test_hash_equal_objects(self) -> None:
        a = GrainId(grain_type="Counter", key="1")
        b = GrainId(grain_type="Counter", key="1")
        assert hash(a) == hash(b)

    def test_hash_different_objects(self) -> None:
        a = GrainId(grain_type="Counter", key="1")
        b = GrainId(grain_type="Counter", key="2")
        assert hash(a) != hash(b)

    def test_frozen(self) -> None:
        gid = GrainId(grain_type="Counter", key="1")
        try:
            gid.key = "changed"  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass

    def test_str(self) -> None:
        gid = GrainId(grain_type="Counter", key="abc")
        assert str(gid) == "Counter/abc"

    def test_usable_in_set(self) -> None:
        a = GrainId(grain_type="Counter", key="1")
        b = GrainId(grain_type="Counter", key="1")
        c = GrainId(grain_type="Counter", key="2")
        s = {a, b, c}
        assert len(s) == 2

    def test_empty_key(self) -> None:
        gid = GrainId(grain_type="Counter", key="")
        assert gid.key == ""
        assert str(gid) == "Counter/"


class TestSiloAddress:
    def test_creation(self) -> None:
        addr = SiloAddress(host="localhost", port=11111, epoch=1000)
        assert addr.host == "localhost"
        assert addr.port == 11111
        assert addr.epoch == 1000

    def test_equality(self) -> None:
        a = SiloAddress(host="localhost", port=11111, epoch=1000)
        b = SiloAddress(host="localhost", port=11111, epoch=1000)
        assert a == b

    def test_inequality_different_epoch(self) -> None:
        a = SiloAddress(host="localhost", port=11111, epoch=1000)
        b = SiloAddress(host="localhost", port=11111, epoch=2000)
        assert a != b

    def test_hashable_and_dict_key(self) -> None:
        addr = SiloAddress(host="localhost", port=11111, epoch=1000)
        d: dict[SiloAddress, str] = {addr: "silo-1"}
        assert d[SiloAddress(host="localhost", port=11111, epoch=1000)] == "silo-1"

    def test_encoded(self) -> None:
        addr = SiloAddress(host="10.0.0.1", port=11111, epoch=1700000000)
        assert addr.encoded == "10.0.0.1_11111_1700000000"

    def test_silo_id_returns_colon_separated_form(self) -> None:
        # Arrange
        addr = SiloAddress(host="10.0.0.1", port=11111, epoch=1700000000)

        # Act
        result = addr.silo_id

        # Assert
        assert result == "10.0.0.1:11111:1700000000"

    def test_less_than_orders_by_silo_id_lexicographically(self) -> None:
        # Arrange
        lower = SiloAddress(host="10.0.0.1", port=11111, epoch=1)
        higher = SiloAddress(host="10.0.0.2", port=11111, epoch=1)

        # Act
        result = lower < higher

        # Assert
        assert result is True

    def test_less_than_is_not_symmetric(self) -> None:
        # Arrange
        a = SiloAddress(host="10.0.0.1", port=11111, epoch=1)
        b = SiloAddress(host="10.0.0.2", port=11111, epoch=1)

        # Act
        result = b < a

        # Assert
        assert result is False

    def test_sorted_is_deterministic(self) -> None:
        # Arrange
        s1 = SiloAddress(host="10.0.0.3", port=11111, epoch=1)
        s2 = SiloAddress(host="10.0.0.1", port=11111, epoch=1)
        s3 = SiloAddress(host="10.0.0.2", port=11111, epoch=1)

        # Act
        result = sorted([s1, s2, s3])

        # Assert
        assert [s.host for s in result] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    def test_frozen(self) -> None:
        addr = SiloAddress(host="localhost", port=11111, epoch=1000)
        try:
            addr.port = 22222  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass

    def test_str(self) -> None:
        addr = SiloAddress(host="localhost", port=11111, epoch=1000)
        assert str(addr) == "localhost:11111"


class TestSiloStatus:
    def test_all_values(self) -> None:
        assert SiloStatus.JOINING.value == "joining"
        assert SiloStatus.ACTIVE.value == "active"
        assert SiloStatus.SHUTTING_DOWN.value == "shutting_down"
        assert SiloStatus.DEAD.value == "dead"

    def test_member_count(self) -> None:
        assert len(SiloStatus) == 4


class TestSiloInfo:
    def test_creation(self) -> None:
        addr = SiloAddress(host="localhost", port=11111, epoch=1000)
        info = SiloInfo(
            address=addr,
            status=SiloStatus.ACTIVE,
            last_heartbeat=1000.0,
            start_time=999.0,
        )
        assert info.address == addr
        assert info.status == SiloStatus.ACTIVE
        assert info.last_heartbeat == 1000.0
        assert info.start_time == 999.0

    def test_mutable(self) -> None:
        addr = SiloAddress(host="localhost", port=11111, epoch=1000)
        info = SiloInfo(
            address=addr,
            status=SiloStatus.JOINING,
            last_heartbeat=1000.0,
            start_time=999.0,
        )
        info.status = SiloStatus.ACTIVE
        info.last_heartbeat = 2000.0
        assert info.status == SiloStatus.ACTIVE
        assert info.last_heartbeat == 2000.0

    def test_phase1_construction_still_works_without_new_fields(self) -> None:
        # Arrange
        addr = SiloAddress(host="localhost", port=11111, epoch=1000)

        # Act
        info = SiloInfo(
            address=addr,
            status=SiloStatus.ACTIVE,
            last_heartbeat=1000.0,
            start_time=999.0,
        )

        # Assert
        assert info.cluster_id is None
        assert info.gateway_port is None
        assert info.version == 0

    def test_cluster_id_accepted_as_keyword(self) -> None:
        # Arrange
        addr = SiloAddress(host="localhost", port=11111, epoch=1000)

        # Act
        info = SiloInfo(
            address=addr,
            status=SiloStatus.ACTIVE,
            last_heartbeat=1000.0,
            start_time=999.0,
            cluster_id="dev",
        )

        # Assert
        assert info.cluster_id == "dev"

    def test_gateway_port_accepted_as_keyword(self) -> None:
        # Arrange
        addr = SiloAddress(host="localhost", port=11111, epoch=1000)

        # Act
        info = SiloInfo(
            address=addr,
            status=SiloStatus.ACTIVE,
            last_heartbeat=1000.0,
            start_time=999.0,
            gateway_port=30000,
        )

        # Assert
        assert info.gateway_port == 30000

    def test_version_accepted_as_keyword(self) -> None:
        # Arrange
        addr = SiloAddress(host="localhost", port=11111, epoch=1000)

        # Act
        info = SiloInfo(
            address=addr,
            status=SiloStatus.ACTIVE,
            last_heartbeat=1000.0,
            start_time=999.0,
            version=7,
        )

        # Assert
        assert info.version == 7
