"""Tests for :mod:`pyleans.cluster.membership_agent`.

Covers:

* ``message_handler`` dispatch for INDIRECT_PROBE and MEMBERSHIP_SNAPSHOT,
* snapshot wire codec round-trip and error handling,
* probe / table-poll / i_am_alive loops driving the right side effects,
* self-terminate hook fires on own DEAD row,
* snapshot broadcast after mutating writes,
* disconnect fast-path bumps miss_count.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from pyleans.cluster.failure_detector import (
    INDIRECT_PROBE_HEADER,
    INDIRECT_PROBE_RESP_FAIL,
    INDIRECT_PROBE_RESP_OK,
    FailureDetector,
    FailureDetectorOptions,
)
from pyleans.cluster.identity import ClusterId
from pyleans.cluster.membership_agent import (
    MEMBERSHIP_SNAPSHOT_HEADER,
    MembershipAgent,
    decode_snapshot,
    encode_snapshot,
)
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus, SuspicionVote
from pyleans.providers.membership import MembershipSnapshot
from pyleans.transport.errors import TransportConnectionError
from pyleans.transport.messages import MessageType, TransportMessage

# Reuse the fakes from test_failure_detector via direct import (they are
# public test helpers and copy-paste would drift over time).
from test_failure_detector import FakeMembership, FakeTransport  # type: ignore[import-not-found]

CLUSTER_ID = ClusterId("test-cluster")


def _silo(
    host: str,
    port: int = 11111,
    epoch: int = 1,
    *,
    status: SiloStatus = SiloStatus.ACTIVE,
    suspicions: list[SuspicionVote] | None = None,
    etag: str | None = "etag-0",
) -> SiloInfo:
    return SiloInfo(
        address=SiloAddress(host=host, port=port, epoch=epoch),
        status=status,
        last_heartbeat=1.0,
        start_time=1.0,
        cluster_id="test-cluster",
        gateway_port=30000,
        suspicions=list(suspicions or []),
        etag=etag,
    )


def _address(host: str, port: int = 11111, epoch: int = 1) -> SiloAddress:
    return SiloAddress(host=host, port=port, epoch=epoch)


def _make_agent(
    local: SiloAddress,
    *,
    options: FailureDetectorOptions | None = None,
    membership: FakeMembership | None = None,
    transport: FakeTransport | None = None,
    wall_clock: Callable[[], float] | None = None,
) -> tuple[MembershipAgent, FailureDetector, FakeTransport, FakeMembership]:
    tx = transport or FakeTransport(local=local)
    mb = membership or FakeMembership()
    detector = FailureDetector(
        local_silo=local,
        cluster_id=CLUSTER_ID,
        options=options or FailureDetectorOptions(),
        transport=tx,
        membership=mb,
        wall_clock=wall_clock or (lambda: 100.0),
    )
    agent = MembershipAgent(
        local_silo=local,
        detector=detector,
        transport=tx,
        membership=mb,
        wall_clock=wall_clock or (lambda: 100.0),
    )
    return agent, detector, tx, mb


# ---- Snapshot wire codec -----------------------------------------------------


class TestSnapshotCodec:
    def test_roundtrip_preserves_fields(self) -> None:
        # Arrange
        snapshot = MembershipSnapshot(
            version=42,
            silos=[
                _silo(
                    "a",
                    suspicions=[SuspicionVote(suspecting_silo="b:11111:1", timestamp=99.5)],
                    etag="e1",
                ),
                _silo("b", status=SiloStatus.DEAD, etag=None),
            ],
        )

        # Act
        wire = encode_snapshot(snapshot)
        restored = decode_snapshot(wire)

        # Assert
        assert restored.version == 42
        assert len(restored.silos) == 2
        assert restored.silos[0].address.host == "a"
        assert restored.silos[0].suspicions[0].suspecting_silo == "b:11111:1"
        assert restored.silos[1].status == SiloStatus.DEAD
        assert restored.silos[1].etag is None

    def test_malformed_bytes_raise_value_error(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="invalid snapshot bytes"):
            decode_snapshot(b"\xff\xfenot json")

    def test_missing_fields_raise_value_error(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="malformed snapshot"):
            decode_snapshot(b'{"version": 1}')

    def test_non_object_raises_value_error(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="JSON object"):
            decode_snapshot(b"[1,2,3]")


# ---- message_handler dispatch ------------------------------------------------


class TestMessageHandler:
    async def test_indirect_probe_request_returns_ok_when_alive(self) -> None:
        # Arrange
        local = _address("local")
        suspect = _address("suspect")
        agent, _, tx, _ = _make_agent(local)
        tx.ping_outcomes[suspect.silo_id] = lambda: 0.005

        # Act
        resp = await agent.message_handler(
            source=_address("other"),
            msg=TransportMessage(
                message_type=MessageType.REQUEST,
                correlation_id=17,
                header=INDIRECT_PROBE_HEADER,
                body=suspect.silo_id.encode(),
            ),
        )

        # Assert
        assert resp is not None
        assert resp.message_type == MessageType.RESPONSE
        assert resp.correlation_id == 17
        assert resp.body == INDIRECT_PROBE_RESP_OK

    async def test_indirect_probe_request_returns_fail_when_dead(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, _ = _make_agent(local)

        # Act
        resp = await agent.message_handler(
            source=_address("other"),
            msg=TransportMessage(
                message_type=MessageType.REQUEST,
                correlation_id=17,
                header=INDIRECT_PROBE_HEADER,
                body=b"unreachable:11111:1",
            ),
        )

        # Assert
        assert resp is not None
        assert resp.body == INDIRECT_PROBE_RESP_FAIL

    async def test_snapshot_one_way_updates_cache(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, _ = _make_agent(local)
        snapshot = MembershipSnapshot(version=7, silos=[_silo("a"), _silo("b")])
        body = encode_snapshot(snapshot)

        # Act
        resp = await agent.message_handler(
            source=_address("a"),
            msg=TransportMessage(
                message_type=MessageType.ONE_WAY,
                correlation_id=0,
                header=MEMBERSHIP_SNAPSHOT_HEADER,
                body=body,
            ),
        )

        # Assert
        assert resp is None
        assert agent.last_snapshot is not None
        assert agent.last_snapshot.version == 7

    async def test_unknown_header_ignored(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, _ = _make_agent(local)

        # Act
        resp = await agent.message_handler(
            source=_address("x"),
            msg=TransportMessage(
                message_type=MessageType.REQUEST,
                correlation_id=1,
                header=b"pyleans/unknown/v1",
                body=b"",
            ),
        )

        # Assert
        assert resp is None

    async def test_malformed_snapshot_does_not_raise(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, _ = _make_agent(local)

        # Act (must not raise)
        resp = await agent.message_handler(
            source=_address("x"),
            msg=TransportMessage(
                message_type=MessageType.ONE_WAY,
                correlation_id=0,
                header=MEMBERSHIP_SNAPSHOT_HEADER,
                body=b"\xff invalid",
            ),
        )

        # Assert
        assert resp is None
        assert agent.last_snapshot is None

    async def test_older_snapshot_is_ignored(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, _ = _make_agent(local)
        fresh = MembershipSnapshot(version=10, silos=[])
        stale = MembershipSnapshot(version=5, silos=[])
        await agent.message_handler(
            source=_address("x"),
            msg=TransportMessage(
                message_type=MessageType.ONE_WAY,
                correlation_id=0,
                header=MEMBERSHIP_SNAPSHOT_HEADER,
                body=encode_snapshot(fresh),
            ),
        )

        # Act
        await agent.message_handler(
            source=_address("x"),
            msg=TransportMessage(
                message_type=MessageType.ONE_WAY,
                correlation_id=0,
                header=MEMBERSHIP_SNAPSHOT_HEADER,
                body=encode_snapshot(stale),
            ),
        )

        # Assert
        assert agent.last_snapshot is not None
        assert agent.last_snapshot.version == 10


# ---- Self-terminate ----------------------------------------------------------


@dataclass
class _Flag:
    fired: bool = False

    async def hit(self) -> None:
        self.fired = True


class TestSelfTermination:
    async def test_hook_fires_when_own_row_is_dead(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, membership = _make_agent(local)
        membership.install(_silo("local", status=SiloStatus.DEAD))
        flag = _Flag()
        agent.on_self_terminate(flag.hit)

        # Act
        await agent._do_probe_cycle()

        # Assert
        assert flag.fired is True
        assert agent.is_running is False

    async def test_hook_fires_via_snapshot_broadcast(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, _ = _make_agent(local)
        flag = _Flag()
        agent.on_self_terminate(flag.hit)
        snapshot = MembershipSnapshot(version=3, silos=[_silo("local", status=SiloStatus.DEAD)])

        # Act
        await agent.message_handler(
            source=_address("x"),
            msg=TransportMessage(
                message_type=MessageType.ONE_WAY,
                correlation_id=0,
                header=MEMBERSHIP_SNAPSHOT_HEADER,
                body=encode_snapshot(snapshot),
            ),
        )

        # Assert
        assert flag.fired is True


# ---- Probe cycle end-to-end --------------------------------------------------


class TestProbeCycle:
    async def test_probe_cycle_pings_active_peers(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, tx, membership = _make_agent(local)
        membership.install(_silo("local"))
        membership.install(_silo("peer"))
        tx.ping_outcomes["peer:11111:1"] = lambda: 0.01

        # Act
        await agent._do_probe_cycle()

        # Assert
        assert any(t.silo_id == "peer:11111:1" for (t, _) in tx.sent_pings)

    async def test_probe_cycle_handles_table_unavailable(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, membership = _make_agent(local)
        membership.unavailable = True

        # Act (must not raise)
        await agent._do_probe_cycle()

        # Assert - no crash, no pings recorded
        assert agent.last_snapshot is None

    async def test_declaration_triggers_snapshot_broadcast(self) -> None:
        # Arrange - peer with prior vote; deciding vote declares dead → broadcast
        local = _address("local")
        peer = _silo(
            "peer",
            suspicions=[SuspicionVote(suspecting_silo="other:11111:1", timestamp=100.0)],
        )
        agent, detector, tx, membership = _make_agent(
            local, options=FailureDetectorOptions(num_missed_probes_limit=1)
        )
        membership.install(_silo("local"))
        membership.install(peer)
        # Connect a peer so there's somewhere to broadcast to
        await tx.connect_to_silo(peer.address)
        detector.peer_health(peer.address.silo_id).miss_count = 0
        tx.ping_outcomes[peer.address.silo_id] = lambda: _raise()

        # Act
        await agent._do_probe_cycle()

        # Assert — snapshot broadcast was attempted to peer
        broadcasts = [(t, h) for (t, h, _) in tx.sent_one_way if h == MEMBERSHIP_SNAPSHOT_HEADER]
        assert len(broadcasts) >= 1


def _raise() -> float:
    from pyleans.transport.errors import TransportTimeoutError

    raise TransportTimeoutError("forced miss")


# ---- IAmAlive loop ------------------------------------------------------------


class TestIAmAliveWrite:
    async def test_i_am_alive_write_updates_own_row(self) -> None:
        # Arrange
        local = _address("local")
        clock_value = [500.0]

        def clock() -> float:
            return clock_value[0]

        agent, _, _, membership = _make_agent(local, wall_clock=clock)
        membership.install(_silo("local"))

        # Act
        await agent._write_i_am_alive()

        # Assert
        assert len(membership.updates) == 1
        updated = membership.updates[0]
        assert updated.i_am_alive == pytest.approx(500.0)
        assert updated.last_heartbeat == pytest.approx(500.0)

    async def test_i_am_alive_missing_row_is_noop(self) -> None:
        # Arrange - no row installed
        local = _address("local")
        agent, _, _, membership = _make_agent(local)

        # Act (must not raise)
        await agent._write_i_am_alive()

        # Assert
        assert membership.updates == []

    async def test_i_am_alive_survives_stale_etag(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, membership = _make_agent(local)
        membership.install(_silo("local"))
        membership.stale_next_writes = 1

        # Act (must not raise)
        await agent._write_i_am_alive()

        # Assert - retry left to next tick; current attempt produces no update
        assert len(membership.updates) == 0

    async def test_i_am_alive_survives_table_unavailable(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, membership = _make_agent(local)
        membership.install(_silo("local"))
        membership.unavailable = True

        # Act (must not raise)
        await agent._write_i_am_alive()

        # Assert
        assert len(membership.updates) == 0


# ---- Disconnect hint ---------------------------------------------------------


class TestDisconnectFastPath:
    async def test_peer_disconnect_bumps_miss_count(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        agent, detector, _, _ = _make_agent(local)

        # Act
        await agent._on_peer_disconnect(peer, None)

        # Assert
        assert detector.peer_health(peer.silo_id).miss_count == 1


# ---- Broadcast best-effort ---------------------------------------------------


class TestBroadcast:
    async def test_broadcast_tolerates_peer_send_failure(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        agent, _, tx, membership = _make_agent(local)
        membership.install(_silo("local"))
        await tx.connect_to_silo(peer)

        # one_way that always raises
        original = tx.send_one_way

        async def broken(p: SiloAddress, h: bytes, b: bytes) -> None:
            del p, h, b
            raise TransportConnectionError("peer gone")

        tx.send_one_way = broken

        # Act (must not raise)
        await agent._broadcast_snapshot()

        # Assert — broadcast completed, nothing crashed
        tx.send_one_way = original
        assert agent.last_snapshot is not None

    async def test_broadcast_skips_local_silo(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, tx, membership = _make_agent(local)
        membership.install(_silo("local"))
        await tx.connect_to_silo(local)  # shouldn't be sent to self

        # Act
        await agent._broadcast_snapshot()

        # Assert
        assert all(t != local for (t, _, _) in tx.sent_one_way)


# ---- Start/stop lifecycle ----------------------------------------------------


class TestLifecycle:
    async def test_start_schedules_three_tasks(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, _ = _make_agent(
            local,
            options=FailureDetectorOptions(
                probe_timeout=3600,
                table_poll_interval=3600,
                i_am_alive_interval=3600,
            ),
        )

        # Act
        await agent.start()
        try:
            # Give the tasks one tick to pick up scheduled state
            await asyncio.sleep(0)
        finally:
            await agent.stop()

        # Assert
        assert agent.is_running is False

    async def test_double_start_raises(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, _ = _make_agent(
            local,
            options=FailureDetectorOptions(
                probe_timeout=3600,
                table_poll_interval=3600,
                i_am_alive_interval=3600,
            ),
        )
        await agent.start()
        try:
            # Act / Assert
            with pytest.raises(RuntimeError, match="already started"):
                await agent.start()
        finally:
            await agent.stop()

    async def test_stop_is_idempotent(self) -> None:
        # Arrange
        local = _address("local")
        agent, _, _, _ = _make_agent(local)

        # Act (must not raise when never started)
        await agent.stop()
        await agent.stop()

        # Assert
        assert agent.is_running is False
