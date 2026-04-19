"""Tests for :mod:`pyleans.cluster.failure_detector`.

Covers every acceptance criterion from task-02-11:

* probe-target selection from hash-ring successors,
* probe success/failure bookkeeping,
* miss-limit triggers suspicion write,
* deciding vote performs indirect probe,
* indirect-probe success aborts, all-failure proceeds with atomic DEAD write,
* :class:`TableStaleError` retry, :class:`MembershipUnavailableError` suspend,
* :class:`SelfMonitor` score widens adjusted probe timeout under stress.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest
from pyleans.cluster.failure_detector import (
    INDIRECT_PROBE_HEADER,
    INDIRECT_PROBE_RESP_FAIL,
    INDIRECT_PROBE_RESP_OK,
    FailureDetector,
    FailureDetectorOptions,
    PeerHealth,
    ProbeResult,
    SelfMonitor,
)
from pyleans.cluster.identity import ClusterId
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus, SuspicionVote
from pyleans.providers.errors import (
    MembershipUnavailableError,
    TableStaleError,
)
from pyleans.providers.membership import (
    MembershipProvider,
    MembershipSnapshot,
)
from pyleans.transport.cluster import (
    ConnectionCallback,
    DisconnectionCallback,
    IClusterTransport,
    MessageHandler,
)
from pyleans.transport.errors import (
    TransportConnectionError,
    TransportError,
    TransportTimeoutError,
)

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


class FakeMembership(MembershipProvider):
    """Tiny in-memory membership implementation for detector tests."""

    def __init__(self) -> None:
        self._rows: dict[str, SiloInfo] = {}
        self._version = 0
        self._etag_counter = 0
        self.unavailable = False
        self.stale_next_writes = 0
        self.updates: list[SiloInfo] = []
        self.added_suspicions: list[tuple[str, SuspicionVote, str]] = []

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return f"etag-{self._etag_counter}"

    def install(self, silo: SiloInfo) -> None:
        row = SiloInfo(
            address=silo.address,
            status=silo.status,
            last_heartbeat=silo.last_heartbeat,
            start_time=silo.start_time,
            cluster_id=silo.cluster_id,
            gateway_port=silo.gateway_port,
            version=silo.version,
            i_am_alive=silo.i_am_alive,
            suspicions=list(silo.suspicions),
            etag=silo.etag or self._next_etag(),
        )
        self._rows[silo.address.silo_id] = row
        self._version += 1

    async def read_all(self) -> MembershipSnapshot:
        if self.unavailable:
            raise MembershipUnavailableError("fake unavailable")
        snapshot_rows = [
            SiloInfo(
                address=r.address,
                status=r.status,
                last_heartbeat=r.last_heartbeat,
                start_time=r.start_time,
                cluster_id=r.cluster_id,
                gateway_port=r.gateway_port,
                version=r.version,
                i_am_alive=r.i_am_alive,
                suspicions=list(r.suspicions),
                etag=r.etag,
            )
            for r in self._rows.values()
        ]
        return MembershipSnapshot(version=self._version, silos=snapshot_rows)

    async def try_update_silo(self, silo: SiloInfo) -> SiloInfo:
        if self.unavailable:
            raise MembershipUnavailableError("fake unavailable")
        if self.stale_next_writes > 0:
            self.stale_next_writes -= 1
            raise TableStaleError("injected stale")
        silo_id = silo.address.silo_id
        current = self._rows.get(silo_id)
        if current is None:
            if silo.etag is not None:
                raise TableStaleError(f"row {silo_id!r} missing")
        elif current.etag != silo.etag:
            raise TableStaleError(f"etag mismatch on {silo_id!r}")
        new_row = SiloInfo(
            address=silo.address,
            status=silo.status,
            last_heartbeat=silo.last_heartbeat,
            start_time=silo.start_time,
            cluster_id=silo.cluster_id,
            gateway_port=silo.gateway_port,
            version=silo.version,
            i_am_alive=silo.i_am_alive,
            suspicions=list(silo.suspicions),
            etag=self._next_etag(),
        )
        self._rows[silo_id] = new_row
        self._version += 1
        self.updates.append(new_row)
        return new_row

    async def try_add_suspicion(
        self, silo_id: str, vote: SuspicionVote, expected_etag: str
    ) -> SiloInfo:
        if self.unavailable:
            raise MembershipUnavailableError("fake unavailable")
        if self.stale_next_writes > 0:
            self.stale_next_writes -= 1
            raise TableStaleError("injected stale")
        current = self._rows.get(silo_id)
        if current is None or current.etag != expected_etag:
            raise TableStaleError(f"etag mismatch or missing row {silo_id!r}")
        updated = SiloInfo(
            address=current.address,
            status=current.status,
            last_heartbeat=current.last_heartbeat,
            start_time=current.start_time,
            cluster_id=current.cluster_id,
            gateway_port=current.gateway_port,
            version=current.version,
            i_am_alive=current.i_am_alive,
            suspicions=[*current.suspicions, vote],
            etag=self._next_etag(),
        )
        self._rows[silo_id] = updated
        self._version += 1
        self.added_suspicions.append((silo_id, vote, expected_etag))
        return updated

    async def try_delete_silo(self, silo: SiloInfo) -> None:
        if self.unavailable:
            raise MembershipUnavailableError("fake unavailable")
        current = self._rows.get(silo.address.silo_id)
        if current is None:
            return
        if current.etag != silo.etag:
            raise TableStaleError("etag mismatch")
        del self._rows[silo.address.silo_id]
        self._version += 1


@dataclass
class FakeTransport(IClusterTransport):
    """Minimal in-memory :class:`IClusterTransport` for detector tests."""

    local: SiloAddress
    ping_outcomes: dict[str, Callable[[], float]] = field(default_factory=dict)
    request_outcomes: dict[tuple[str, bytes], Callable[[], tuple[bytes, bytes]]] = field(
        default_factory=dict,
    )
    connected_silos: list[SiloAddress] = field(default_factory=list)
    sent_pings: list[tuple[SiloAddress, float]] = field(default_factory=list)
    sent_requests: list[tuple[SiloAddress, bytes, bytes, float | None]] = field(
        default_factory=list,
    )
    sent_one_way: list[tuple[SiloAddress, bytes, bytes]] = field(default_factory=list)
    conn_lost_callbacks: list[DisconnectionCallback] = field(default_factory=list)
    conn_up_callbacks: list[ConnectionCallback] = field(default_factory=list)

    async def start(
        self,
        local_silo: SiloAddress,
        cluster_id: ClusterId,
        message_handler: MessageHandler,
    ) -> None:
        del local_silo, cluster_id, message_handler

    async def stop(self) -> None:
        pass

    async def connect_to_silo(self, silo: SiloAddress) -> None:
        if silo not in self.connected_silos:
            self.connected_silos.append(silo)

    async def disconnect_from_silo(self, silo: SiloAddress) -> None:
        if silo in self.connected_silos:
            self.connected_silos.remove(silo)

    async def send_request(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        self.sent_requests.append((target, header, body, timeout))
        outcome = self.request_outcomes.get((target.silo_id, header))
        if outcome is None:
            raise TransportConnectionError(f"no reply configured for {target.silo_id}")
        return outcome()

    async def send_one_way(self, target: SiloAddress, header: bytes, body: bytes) -> None:
        self.sent_one_way.append((target, header, body))

    async def send_ping(self, target: SiloAddress, timeout: float = 10.0) -> float:
        self.sent_pings.append((target, timeout))
        outcome = self.ping_outcomes.get(target.silo_id)
        if outcome is None:
            raise TransportConnectionError(f"unreachable {target.silo_id}")
        return outcome()

    def is_connected_to(self, silo: SiloAddress) -> bool:
        return silo in self.connected_silos

    def get_connected_silos(self) -> list[SiloAddress]:
        return list(self.connected_silos)

    @property
    def local_silo(self) -> SiloAddress:
        return self.local

    def on_connection_established(self, callback: ConnectionCallback) -> None:
        self.conn_up_callbacks.append(callback)

    def on_connection_lost(self, callback: DisconnectionCallback) -> None:
        self.conn_lost_callbacks.append(callback)


def _make_detector(
    local: SiloAddress,
    *,
    options: FailureDetectorOptions | None = None,
    membership: FakeMembership | None = None,
    transport: FakeTransport | None = None,
    wall_clock: Callable[[], float] | None = None,
    intermediary_sampler: Callable[[Sequence[SiloAddress], int], list[SiloAddress]] | None = None,
) -> tuple[FailureDetector, FakeTransport, FakeMembership]:
    tx = transport or FakeTransport(local=local)
    mb = membership or FakeMembership()
    detector = FailureDetector(
        local_silo=local,
        cluster_id=CLUSTER_ID,
        options=options or FailureDetectorOptions(),
        transport=tx,
        membership=mb,
        wall_clock=wall_clock or (lambda: 100.0),
        intermediary_sampler=intermediary_sampler,
    )
    return detector, tx, mb


# ---- FailureDetectorOptions validation --------------------------------------


class TestFailureDetectorOptions:
    def test_defaults_match_orleans(self) -> None:
        # Act
        opts = FailureDetectorOptions()

        # Assert
        assert opts.num_probed_silos == 3
        assert opts.num_votes_for_death_declaration == 2
        assert opts.num_missed_probes_limit == 3

    def test_rejects_negative_counts(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="num_probed_silos"):
            FailureDetectorOptions(num_probed_silos=-1)

    def test_rejects_non_positive_intervals(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="probe_timeout"):
            FailureDetectorOptions(probe_timeout=0)

    def test_rejects_zero_votes_required(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="num_votes_for_death_declaration"):
            FailureDetectorOptions(num_votes_for_death_declaration=0)


# ---- SelfMonitor -------------------------------------------------------------


class TestSelfMonitor:
    def test_score_starts_at_zero(self) -> None:
        # Arrange / Act
        monitor = SelfMonitor(FailureDetectorOptions())

        # Assert
        assert monitor.score == 0

    def test_adjusted_probe_timeout_unchanged_when_healthy(self) -> None:
        # Arrange
        opts = FailureDetectorOptions(probe_timeout_per_attempt=5.0)
        monitor = SelfMonitor(opts)

        # Act
        timeout = monitor.adjusted_probe_timeout()

        # Assert
        assert timeout == pytest.approx(5.0)

    def test_heavy_loop_drift_widens_timeout(self) -> None:
        # Arrange
        opts = FailureDetectorOptions(probe_timeout_per_attempt=4.0)
        monitor = SelfMonitor(opts)

        # Act
        for _ in range(3):
            monitor.record_loop_drift(2.0)

        # Assert - score clamped to max 8, timeout = 4.0 * (1 + 8/4) = 4.0 * 3 = 12.0
        assert monitor.score == 8
        assert monitor.adjusted_probe_timeout() == pytest.approx(12.0)

    def test_probe_send_failures_contribute_to_score(self) -> None:
        # Arrange
        monitor = SelfMonitor(FailureDetectorOptions())

        # Act
        for _ in range(5):
            monitor.record_probe_send_failure()

        # Assert
        assert monitor.score == 5

    def test_healthy_ticks_decay_drift(self) -> None:
        # Arrange
        monitor = SelfMonitor(FailureDetectorOptions())
        monitor.record_loop_drift(2.0)
        monitor.record_loop_drift(2.0)  # score 6 (capped add)
        score_before = monitor.score

        # Act
        for _ in range(score_before + 2):
            monitor.record_loop_drift(0.0)

        # Assert
        assert monitor.score == 0

    def test_probe_send_reset_decays(self) -> None:
        # Arrange
        monitor = SelfMonitor(FailureDetectorOptions())
        for _ in range(4):
            monitor.record_probe_send_failure()

        # Act
        monitor.reset_probe_send_failure()

        # Assert
        assert monitor.score == 3


# ---- Probe-target selection --------------------------------------------------


class TestProbeTargets:
    def test_single_silo_cluster_has_no_targets(self) -> None:
        # Arrange
        local = _address("local")
        detector, _, membership = _make_detector(local)
        membership.install(_silo("local"))

        # Act
        targets = detector.probe_targets(MembershipSnapshot(version=1, silos=[_silo("local")]))

        # Assert
        assert targets == []

    def test_small_cluster_probes_all_other_active_peers(self) -> None:
        # Arrange
        local = _address("a", port=10)
        detector, _, _ = _make_detector(local, options=FailureDetectorOptions(num_probed_silos=3))
        snapshot = MembershipSnapshot(
            version=1,
            silos=[
                _silo("a", port=10),
                _silo("b", port=10),
                _silo("c", port=10),
            ],
        )

        # Act
        targets = detector.probe_targets(snapshot)

        # Assert
        assert {t.host for t in targets} == {"b", "c"}

    def test_dead_peers_not_probed(self) -> None:
        # Arrange
        local = _address("a")
        detector, _, _ = _make_detector(local)
        snapshot = MembershipSnapshot(
            version=1,
            silos=[
                _silo("a"),
                _silo("b", status=SiloStatus.DEAD),
                _silo("c"),
            ],
        )

        # Act
        targets = detector.probe_targets(snapshot)

        # Assert
        assert [t.host for t in targets] == ["c"]

    def test_large_cluster_uses_ring_successors(self) -> None:
        # Arrange - 10 silos, probe_count=3 → picks ring successors deterministically
        local = _address("a0")
        detector, _, _ = _make_detector(local, options=FailureDetectorOptions(num_probed_silos=3))
        snapshot = MembershipSnapshot(
            version=1,
            silos=[_silo(f"a{i}") for i in range(10)],
        )

        # Act
        targets_a = detector.probe_targets(snapshot)
        targets_b = detector.probe_targets(snapshot)

        # Assert
        assert len(targets_a) == 3
        assert targets_a == targets_b  # deterministic across calls


# ---- on_probe_result: recovery + miss threshold ------------------------------


class TestProbeResultBookkeeping:
    async def test_successful_probe_resets_miss_count(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        detector, _, _ = _make_detector(local)
        detector.peer_health(peer.silo_id).miss_count = 2

        # Act
        declared = await detector.on_probe_result(
            peer, ProbeResult(ok=True, rtt=0.01), MembershipSnapshot(version=1, silos=[])
        )

        # Assert
        assert declared is False
        assert detector.peer_health(peer.silo_id).miss_count == 0
        assert detector.peer_health(peer.silo_id).last_rtt == pytest.approx(0.01)

    async def test_failed_probe_increments_miss_count(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        detector, _, _ = _make_detector(local)

        # Act
        declared = await detector.on_probe_result(
            peer,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=1, silos=[]),
        )

        # Assert
        assert declared is False
        assert detector.peer_health(peer.silo_id).miss_count == 1


# ---- Voting state machine ----------------------------------------------------


class TestVotingFlow:
    async def test_limit_misses_triggers_suspicion_write(self) -> None:
        # Arrange - only local voted so far; deciding requires 2 → not yet deciding
        local = _address("local")
        peer = _silo("peer")
        detector, _, membership = _make_detector(
            local,
            options=FailureDetectorOptions(
                num_missed_probes_limit=2,
                num_votes_for_death_declaration=2,
            ),
        )
        membership.install(peer)
        # Prime with num_missed_probes_limit-1 misses
        for _ in range(1):
            await detector.on_probe_result(
                peer.address,
                ProbeResult(ok=False, reason="timeout"),
                MembershipSnapshot(version=0, silos=[]),
            )

        # Act — the miss-limit-th miss triggers the vote path
        declared = await detector.on_probe_result(
            peer.address,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=0, silos=[]),
        )

        # Assert — one suspicion appended, row NOT flipped to DEAD
        assert declared is False
        assert len(membership.added_suspicions) == 1
        suspicion_target, vote, _expected_etag = membership.added_suspicions[0]
        assert suspicion_target == peer.address.silo_id
        assert vote.suspecting_silo == local.silo_id

    async def test_deciding_vote_with_no_intermediaries_declares_dead(self) -> None:
        # Arrange - pre-existing vote from another silo, so our vote is deciding;
        # no third silo exists → no intermediaries → direct to DEAD.
        local = _address("local")
        peer = _silo(
            "peer",
            suspicions=[SuspicionVote(suspecting_silo="other:11111:1", timestamp=100.0)],
        )
        detector, _, membership = _make_detector(
            local,
            options=FailureDetectorOptions(num_missed_probes_limit=1),
        )
        membership.install(peer)

        # Act
        declared = await detector.on_probe_result(
            peer.address,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=0, silos=[]),
        )

        # Assert
        assert declared is True
        assert len(membership.updates) == 1
        assert membership.updates[0].status == SiloStatus.DEAD
        assert any(v.suspecting_silo == local.silo_id for v in membership.updates[0].suspicions)

    async def test_indirect_probe_alive_aborts_death_vote(self) -> None:
        # Arrange - 3 active silos; peer has a prior vote; intermediary confirms alive
        local = _address("local")
        peer = _silo(
            "peer",
            suspicions=[SuspicionVote(suspecting_silo="other:11111:1", timestamp=100.0)],
        )
        mediator = _silo("mediator")
        detector, tx, membership = _make_detector(
            local,
            options=FailureDetectorOptions(
                num_missed_probes_limit=1,
                indirect_probe_intermediaries=1,
            ),
        )
        membership.install(_silo("local"))
        membership.install(peer)
        membership.install(mediator)
        tx.request_outcomes[(mediator.address.silo_id, INDIRECT_PROBE_HEADER)] = lambda: (
            INDIRECT_PROBE_HEADER,
            INDIRECT_PROBE_RESP_OK,
        )

        # Act
        declared = await detector.on_probe_result(
            peer.address,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=0, silos=[]),
        )

        # Assert - aborted, no status flip, suspicion still added
        assert declared is False
        assert not any(u.status == SiloStatus.DEAD for u in membership.updates)
        assert len(membership.added_suspicions) == 1

    async def test_indirect_probe_all_fail_declares_dead(self) -> None:
        # Arrange - 3 active silos; peer has prior vote; mediator reports FAIL
        local = _address("local")
        peer = _silo(
            "peer",
            suspicions=[SuspicionVote(suspecting_silo="other:11111:1", timestamp=100.0)],
        )
        mediator = _silo("mediator")
        detector, tx, membership = _make_detector(
            local,
            options=FailureDetectorOptions(
                num_missed_probes_limit=1,
                indirect_probe_intermediaries=1,
            ),
        )
        membership.install(_silo("local"))
        membership.install(peer)
        membership.install(mediator)
        tx.request_outcomes[(mediator.address.silo_id, INDIRECT_PROBE_HEADER)] = lambda: (
            INDIRECT_PROBE_HEADER,
            INDIRECT_PROBE_RESP_FAIL,
        )

        # Act
        declared = await detector.on_probe_result(
            peer.address,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=0, silos=[]),
        )

        # Assert
        assert declared is True
        assert any(u.status == SiloStatus.DEAD for u in membership.updates)

    async def test_indirect_probe_request_raising_counts_as_failure(self) -> None:
        # Arrange
        local = _address("local")
        peer = _silo(
            "peer",
            suspicions=[SuspicionVote(suspecting_silo="other:11111:1", timestamp=100.0)],
        )
        mediator = _silo("mediator")
        detector, tx, membership = _make_detector(
            local,
            options=FailureDetectorOptions(
                num_missed_probes_limit=1,
                indirect_probe_intermediaries=1,
            ),
        )
        membership.install(peer)
        membership.install(mediator)

        def boom() -> tuple[bytes, bytes]:
            raise TransportTimeoutError("mediator timeout")

        tx.request_outcomes[(mediator.address.silo_id, INDIRECT_PROBE_HEADER)] = boom

        # Act
        declared = await detector.on_probe_result(
            peer.address,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=0, silos=[]),
        )

        # Assert - raising mediator == no positive confirmation → proceed with DEAD
        assert declared is True

    async def test_double_vote_suppressed_when_fresh_vote_exists(self) -> None:
        # Arrange - peer already has our own fresh vote
        local = _address("local")
        peer = _silo(
            "peer",
            suspicions=[SuspicionVote(suspecting_silo=local.silo_id, timestamp=100.0)],
        )
        detector, _, membership = _make_detector(
            local, options=FailureDetectorOptions(num_missed_probes_limit=1)
        )
        membership.install(peer)

        # Act
        declared = await detector.on_probe_result(
            peer.address,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=0, silos=[]),
        )

        # Assert - no new write at all
        assert declared is False
        assert membership.added_suspicions == []
        assert membership.updates == []

    async def test_expired_votes_not_counted(self) -> None:
        # Arrange - existing vote is older than death_vote_expiration; ours should NOT be deciding
        local = _address("local")
        peer = _silo(
            "peer",
            suspicions=[SuspicionVote(suspecting_silo="old:11111:1", timestamp=0.0)],
        )
        detector, _, membership = _make_detector(
            local,
            options=FailureDetectorOptions(
                num_missed_probes_limit=1,
                death_vote_expiration=50.0,
            ),
            wall_clock=lambda: 1000.0,  # well past expiration
        )
        membership.install(peer)

        # Act
        declared = await detector.on_probe_result(
            peer.address,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=0, silos=[]),
        )

        # Assert - expired votes ignored; only our vote counts → not deciding
        assert declared is False
        assert len(membership.added_suspicions) == 1

    async def test_peer_already_dead_skips_write(self) -> None:
        # Arrange
        local = _address("local")
        peer = _silo("peer", status=SiloStatus.DEAD)
        detector, _, membership = _make_detector(
            local, options=FailureDetectorOptions(num_missed_probes_limit=1)
        )
        membership.install(peer)

        # Act
        declared = await detector.on_probe_result(
            peer.address,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=0, silos=[]),
        )

        # Assert
        assert declared is True
        assert membership.added_suspicions == []
        assert membership.updates == []


# ---- Error handling: stale + unavailable -------------------------------------


class TestVoteRetry:
    async def test_table_stale_triggers_reread(self) -> None:
        # Arrange - first write fails with stale, second succeeds
        local = _address("local")
        peer = _silo("peer")
        detector, _, membership = _make_detector(
            local, options=FailureDetectorOptions(num_missed_probes_limit=1)
        )
        membership.install(peer)
        membership.stale_next_writes = 1

        # Act
        declared = await detector.on_probe_result(
            peer.address,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=0, silos=[]),
        )

        # Assert
        assert declared is False
        assert len(membership.added_suspicions) == 1

    async def test_membership_unavailable_suspends_voting(self) -> None:
        # Arrange
        local = _address("local")
        peer = _silo("peer")
        detector, _, membership = _make_detector(
            local, options=FailureDetectorOptions(num_missed_probes_limit=1)
        )
        membership.install(peer)
        membership.unavailable = True

        # Act
        declared = await detector.on_probe_result(
            peer.address,
            ProbeResult(ok=False, reason="timeout"),
            MembershipSnapshot(version=0, silos=[]),
        )

        # Assert - no vote, no crash
        assert declared is False
        assert membership.added_suspicions == []
        assert membership.updates == []


# ---- probe_once: transport error classification ------------------------------


class TestProbeOnceErrorClassification:
    async def test_ping_timeout_becomes_probe_result_reason(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        detector, tx, _ = _make_detector(local)

        def timeout_ping() -> float:
            raise TransportTimeoutError("per-attempt timeout")

        tx.ping_outcomes[peer.silo_id] = timeout_ping

        # Act
        result = await detector.probe_once(peer)

        # Assert
        assert result.ok is False
        assert result.reason == "timeout"

    async def test_connection_error_produces_reason(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        detector, tx, _ = _make_detector(local)

        def disconnected() -> float:
            raise TransportConnectionError("peer gone")

        tx.ping_outcomes[peer.silo_id] = disconnected

        # Act
        result = await detector.probe_once(peer)

        # Assert
        assert result.ok is False
        assert result.reason is not None
        assert "connection" in result.reason

    async def test_transport_error_records_self_failure(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        detector, tx, _ = _make_detector(local)

        def generic_transport_failure() -> float:
            raise TransportError("transport is misbehaving")

        tx.ping_outcomes[peer.silo_id] = generic_transport_failure

        # Act
        result = await detector.probe_once(peer)

        # Assert
        assert result.ok is False
        assert detector.self_monitor.score > 0

    async def test_successful_probe_resets_self_failure(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        detector, tx, _ = _make_detector(local)
        detector.self_monitor.record_probe_send_failure()
        detector.self_monitor.record_probe_send_failure()
        tx.ping_outcomes[peer.silo_id] = lambda: 0.01

        # Act
        result = await detector.probe_once(peer)

        # Assert
        assert result.ok is True
        assert detector.self_monitor.score == 1  # one decremented


# ---- INDIRECT_PROBE handler --------------------------------------------------


class TestIndirectProbeHandler:
    async def test_handler_returns_ok_when_direct_probe_succeeds(self) -> None:
        # Arrange
        local = _address("local")
        suspect = _address("suspect")
        detector, tx, _ = _make_detector(local)
        tx.ping_outcomes[suspect.silo_id] = lambda: 0.005

        # Act
        body = await detector.handle_indirect_probe_request(suspect.silo_id.encode())

        # Assert
        assert body == INDIRECT_PROBE_RESP_OK

    async def test_handler_returns_fail_when_direct_probe_fails(self) -> None:
        # Arrange
        local = _address("local")
        detector, _, _ = _make_detector(local)

        # Act
        body = await detector.handle_indirect_probe_request(b"peer:11111:1")

        # Assert
        assert body == INDIRECT_PROBE_RESP_FAIL

    async def test_handler_returns_fail_on_malformed_body(self) -> None:
        # Arrange
        local = _address("local")
        detector, _, _ = _make_detector(local)

        # Act
        body = await detector.handle_indirect_probe_request(b"\xff\xfeINVALID")

        # Assert
        assert body == INDIRECT_PROBE_RESP_FAIL


# ---- bump_miss_count on disconnect hint --------------------------------------


class TestDisconnectHint:
    def test_bump_miss_count_caps_at_limit(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        detector, _, _ = _make_detector(
            local, options=FailureDetectorOptions(num_missed_probes_limit=3)
        )

        # Act
        for _ in range(10):
            detector.bump_miss_count(peer.silo_id)

        # Assert
        assert detector.peer_health(peer.silo_id).miss_count == 3


# ---- PeerHealth defaults -----------------------------------------------------


class TestPeerHealth:
    def test_defaults(self) -> None:
        # Arrange / Act
        health = PeerHealth()

        # Assert
        assert health.miss_count == 0
        assert health.consecutive_ok == 0
        assert health.last_probe_ok_time == 0.0
        assert health.last_rtt == 0.0


# ---- pick_intermediaries -----------------------------------------------------


class TestPickIntermediaries:
    def test_excludes_local_and_peer(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        detector, _, _ = _make_detector(
            local,
            options=FailureDetectorOptions(indirect_probe_intermediaries=2),
            intermediary_sampler=lambda pop, k: list(pop)[:k],
        )
        snapshot = MembershipSnapshot(
            version=1,
            silos=[
                _silo("local"),
                _silo("peer"),
                _silo("m1"),
                _silo("m2"),
                _silo("m3"),
            ],
        )

        # Act
        intermediaries = detector.pick_intermediaries(snapshot, peer)

        # Assert
        assert local not in intermediaries
        assert peer not in intermediaries
        assert len(intermediaries) == 2

    def test_empty_when_no_candidates(self) -> None:
        # Arrange
        local = _address("local")
        peer = _address("peer")
        detector, _, _ = _make_detector(local)
        snapshot = MembershipSnapshot(version=1, silos=[_silo("local"), _silo("peer")])

        # Act
        intermediaries = detector.pick_intermediaries(snapshot, peer)

        # Assert
        assert intermediaries == []
