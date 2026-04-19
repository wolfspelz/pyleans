"""Tests for :mod:`pyleans.cluster.directory_rebuild`.

Covers the rebuild coordinator's pure-logic surface: arc computation
from old vs new rings, merge-by-epoch conflict resolution,
DEACTIVATE emission for losers, grace-period coalescing of rapid
membership changes, cancellation safety, and partial-peer failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from pyleans.cluster.directory import DirectoryEntry
from pyleans.cluster.directory_messages import (
    DIRECTORY_REBUILD_QUERY_HEADER,
    Arc,
    RebuildQueryRequest,
    RebuildQueryResponse,
    decode_rebuild_query,
    encode_rebuild_response,
)
from pyleans.cluster.directory_rebuild import (
    DirectoryRebuildCoordinator,
    compute_new_arcs,
    merge_rebuild_candidates,
)
from pyleans.cluster.hash_ring import ConsistentHashRing
from pyleans.cluster.identity import ClusterId
from pyleans.identity import GrainId, SiloAddress
from pyleans.transport.cluster import (
    ConnectionCallback,
    DisconnectionCallback,
    IClusterTransport,
    MessageHandler,
)
from pyleans.transport.errors import TransportConnectionError


def _silos() -> list[SiloAddress]:
    return [
        SiloAddress(host="a", port=11111, epoch=1),
        SiloAddress(host="b", port=11111, epoch=1),
        SiloAddress(host="c", port=11111, epoch=1),
    ]


@dataclass
class FakeRebuildTransport(IClusterTransport):
    """Transport that answers REBUILD_QUERY from a canned per-peer response map."""

    local: SiloAddress
    peer_responses: dict[str, Callable[[RebuildQueryRequest], RebuildQueryResponse]] = field(
        default_factory=dict
    )
    peer_raises: dict[str, Callable[[], Exception]] = field(default_factory=dict)
    query_log: list[tuple[SiloAddress, RebuildQueryRequest]] = field(default_factory=list)
    one_way_log: list[tuple[SiloAddress, bytes, bytes]] = field(default_factory=list)

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
        pass

    async def disconnect_from_silo(self, silo: SiloAddress) -> None:
        pass

    async def send_request(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        del timeout
        if header != DIRECTORY_REBUILD_QUERY_HEADER:
            raise TransportConnectionError(f"unexpected header {header!r}")
        req = decode_rebuild_query(body)
        self.query_log.append((target, req))
        if target.silo_id in self.peer_raises:
            raise self.peer_raises[target.silo_id]()
        handler = self.peer_responses.get(target.silo_id)
        if handler is None:
            raise TransportConnectionError(f"no response for {target.silo_id}")
        resp = handler(req)
        return header, encode_rebuild_response(resp)

    async def send_one_way(self, target: SiloAddress, header: bytes, body: bytes) -> None:
        self.one_way_log.append((target, header, body))

    async def send_ping(self, target: SiloAddress, timeout: float = 10.0) -> float:
        del target, timeout
        return 0.001

    def is_connected_to(self, silo: SiloAddress) -> bool:
        return True

    def get_connected_silos(self) -> list[SiloAddress]:
        return []

    @property
    def local_silo(self) -> SiloAddress:
        return self.local

    def on_connection_established(self, callback: ConnectionCallback) -> None:
        pass

    def on_connection_lost(self, callback: DisconnectionCallback) -> None:
        pass


# ---- Arc helpers ------------------------------------------------------------


class TestArcContains:
    def test_forward_arc_excludes_start_includes_end(self) -> None:
        # Arrange
        arc = Arc(start=100, end=200)

        # Act / Assert
        assert not arc.contains(100)
        assert arc.contains(150)
        assert arc.contains(200)
        assert not arc.contains(250)

    def test_wrapping_arc_spans_zero(self) -> None:
        # Arrange - arc wraps from near-max to small
        arc = Arc(start=(1 << 63), end=10)

        # Act / Assert
        assert arc.contains(0)
        assert arc.contains(10)
        assert arc.contains((1 << 63) + 1)
        assert not arc.contains(11)
        assert not arc.contains(1 << 63)


class TestComputeNewArcs:
    def test_empty_new_ring_returns_no_arcs(self) -> None:
        # Arrange
        local = _silos()[0]

        # Act
        arcs = compute_new_arcs(
            old_ring=None,
            new_ring=ConsistentHashRing([]),
            local_silo=local,
        )

        # Assert
        assert arcs == []

    def test_fresh_ring_returns_all_arcs_for_local(self) -> None:
        # Arrange
        silos = _silos()
        local = silos[0]
        new_ring = ConsistentHashRing(silos)

        # Act - no old ring means every arc on new ring is "new"
        arcs = compute_new_arcs(old_ring=None, new_ring=new_ring, local_silo=local)

        # Assert - one arc per virtual node owned by local
        assert len(arcs) > 0
        assert len(arcs) == sum(1 for p in new_ring.positions if p.silo == local)


class TestMergeByEpoch:
    def test_single_candidate_wins(self) -> None:
        # Arrange
        gid = GrainId("T", "a")
        entry = DirectoryEntry(gid, _silos()[0], activation_epoch=3)

        # Act
        winners, losers = merge_rebuild_candidates([entry])

        # Assert
        assert winners == {gid: entry}
        assert losers == []

    def test_lower_epoch_wins_against_existing(self) -> None:
        # Arrange
        gid = GrainId("T", "a")
        existing = DirectoryEntry(gid, _silos()[0], activation_epoch=5)
        incoming = DirectoryEntry(gid, _silos()[1], activation_epoch=2)

        # Act
        winners, losers = merge_rebuild_candidates([incoming], existing={gid: existing})

        # Assert
        assert winners[gid] == incoming
        assert losers == [(existing.silo, gid, existing.activation_epoch)]

    def test_higher_epoch_loses_to_existing(self) -> None:
        # Arrange
        gid = GrainId("T", "a")
        existing = DirectoryEntry(gid, _silos()[0], activation_epoch=2)
        incoming = DirectoryEntry(gid, _silos()[1], activation_epoch=9)

        # Act
        winners, losers = merge_rebuild_candidates([incoming], existing={gid: existing})

        # Assert
        assert winners[gid] == existing
        assert losers == [(incoming.silo, gid, incoming.activation_epoch)]

    def test_tie_breaks_to_lex_smaller_silo_id(self) -> None:
        # Arrange - same epoch, different silos
        gid = GrainId("T", "a")
        silo_a, silo_b, _ = _silos()
        existing = DirectoryEntry(gid, silo_b, activation_epoch=3)
        incoming = DirectoryEntry(gid, silo_a, activation_epoch=3)

        # Act
        winners, losers = merge_rebuild_candidates([incoming], existing={gid: existing})

        # Assert - incoming silo_a < silo_b → incoming wins
        assert winners[gid] == incoming
        assert len(losers) == 1


# ---- Coordinator ------------------------------------------------------------


class TestCoordinator:
    async def test_schedule_rebuild_applies_merged_entries(self) -> None:
        # Arrange
        local, peer1, peer2 = _silos()
        tx = FakeRebuildTransport(local=local)
        applied: list[dict[GrainId, DirectoryEntry]] = []

        async def apply(entries: dict[GrainId, DirectoryEntry]) -> None:
            applied.append(entries)

        async def deactivate(silo: SiloAddress, gid: GrainId, epoch: int) -> None:
            del silo, gid, epoch

        coordinator = DirectoryRebuildCoordinator(
            local_silo=local,
            transport=tx,
            apply_entries=apply,
            deactivate_duplicate=deactivate,
            grace_period=0.0,
        )

        gid = GrainId("T", "g1")
        peer1_entry = DirectoryEntry(gid, peer1, activation_epoch=7)
        tx.peer_responses[peer1.silo_id] = lambda _: RebuildQueryResponse(entries=[peer1_entry])
        tx.peer_responses[peer2.silo_id] = lambda _: RebuildQueryResponse(entries=[])

        # Act
        await coordinator.schedule_rebuild(arcs=[Arc(0, (1 << 64) - 1)], peers=[peer1, peer2])
        await coordinator._active_task

        # Assert
        assert applied == [{gid: peer1_entry}]

    async def test_partial_peer_failure_does_not_block_rebuild(self) -> None:
        # Arrange
        local, peer1, peer2 = _silos()
        tx = FakeRebuildTransport(local=local)
        applied: list[dict[GrainId, DirectoryEntry]] = []

        async def apply(entries: dict[GrainId, DirectoryEntry]) -> None:
            applied.append(entries)

        async def deactivate(*_args: object) -> None:
            pass

        coordinator = DirectoryRebuildCoordinator(
            local_silo=local,
            transport=tx,
            apply_entries=apply,
            deactivate_duplicate=deactivate,
            grace_period=0.0,
        )

        gid = GrainId("T", "g2")
        peer2_entry = DirectoryEntry(gid, peer2, activation_epoch=1)

        def peer1_raise() -> Exception:
            return TransportConnectionError("peer1 unreachable")

        tx.peer_raises[peer1.silo_id] = peer1_raise
        tx.peer_responses[peer2.silo_id] = lambda _: RebuildQueryResponse(entries=[peer2_entry])

        # Act
        await coordinator.schedule_rebuild(arcs=[Arc(0, (1 << 64) - 1)], peers=[peer1, peer2])
        await coordinator._active_task

        # Assert - peer2's entry was merged even though peer1 errored
        assert applied == [{gid: peer2_entry}]

    async def test_rebuild_emits_deactivate_for_losers(self) -> None:
        # Arrange
        local, peer1, peer2 = _silos()
        tx = FakeRebuildTransport(local=local)
        deactivate_log: list[tuple[SiloAddress, GrainId, int]] = []

        async def apply(entries: dict[GrainId, DirectoryEntry]) -> None:
            del entries

        async def deactivate(silo: SiloAddress, gid: GrainId, epoch: int) -> None:
            deactivate_log.append((silo, gid, epoch))

        coordinator = DirectoryRebuildCoordinator(
            local_silo=local,
            transport=tx,
            apply_entries=apply,
            deactivate_duplicate=deactivate,
            grace_period=0.0,
        )

        gid = GrainId("T", "dup")
        loser = DirectoryEntry(gid, peer1, activation_epoch=5)
        winner = DirectoryEntry(gid, peer2, activation_epoch=1)
        tx.peer_responses[peer1.silo_id] = lambda _: RebuildQueryResponse(entries=[loser])
        tx.peer_responses[peer2.silo_id] = lambda _: RebuildQueryResponse(entries=[winner])

        # Act
        await coordinator.schedule_rebuild(arcs=[Arc(0, (1 << 64) - 1)], peers=[peer1, peer2])
        await coordinator._active_task

        # Assert - loser receives DEACTIVATE
        assert len(deactivate_log) == 1
        loser_silo, loser_gid, loser_epoch = deactivate_log[0]
        assert loser_gid == gid
        assert loser_epoch == 5
        assert loser_silo == peer1

    async def test_grace_period_coalesces_rapid_reschedules(self) -> None:
        # Arrange
        local, peer1, _ = _silos()
        tx = FakeRebuildTransport(local=local)
        applied: list[dict[GrainId, DirectoryEntry]] = []

        async def apply(entries: dict[GrainId, DirectoryEntry]) -> None:
            applied.append(entries)

        async def deactivate(*_args: object) -> None:
            pass

        coordinator = DirectoryRebuildCoordinator(
            local_silo=local,
            transport=tx,
            apply_entries=apply,
            deactivate_duplicate=deactivate,
            grace_period=0.05,
        )

        tx.peer_responses[peer1.silo_id] = lambda _: RebuildQueryResponse(entries=[])

        # Act - schedule twice rapidly; first is cancelled by second
        await coordinator.schedule_rebuild(arcs=[Arc(0, 1000)], peers=[peer1])
        await coordinator.schedule_rebuild(arcs=[Arc(2000, 3000)], peers=[peer1])
        await coordinator._active_task

        # Assert - only the second rebuild's query landed on peer1
        assert len(tx.query_log) == 1
        assert tx.query_log[0][1].arcs == [Arc(2000, 3000)]

    async def test_cancel_leaves_coordinator_safe_to_reuse(self) -> None:
        # Arrange
        local, peer1, _ = _silos()
        tx = FakeRebuildTransport(local=local)

        async def apply(entries: dict[GrainId, DirectoryEntry]) -> None:
            del entries

        async def deactivate(*_args: object) -> None:
            pass

        coordinator = DirectoryRebuildCoordinator(
            local_silo=local,
            transport=tx,
            apply_entries=apply,
            deactivate_duplicate=deactivate,
            grace_period=5.0,  # large; we'll cancel before it fires
        )
        tx.peer_responses[peer1.silo_id] = lambda _: RebuildQueryResponse(entries=[])

        # Act - schedule then immediately cancel
        await coordinator.schedule_rebuild(arcs=[Arc(0, 100)], peers=[peer1])
        await coordinator.cancel()

        # Assert - coordinator is idle and can be scheduled again
        assert not coordinator.is_rebuilding
        coordinator_grace_0 = DirectoryRebuildCoordinator(
            local_silo=local,
            transport=tx,
            apply_entries=apply,
            deactivate_duplicate=deactivate,
            grace_period=0.0,
        )
        await coordinator_grace_0.schedule_rebuild(arcs=[Arc(0, 100)], peers=[peer1])
        await coordinator_grace_0._active_task
        assert not coordinator_grace_0.is_rebuilding


class TestConstruction:
    def test_rejects_negative_grace_period(self) -> None:
        # Arrange
        local = _silos()[0]
        tx = FakeRebuildTransport(local=local)

        async def apply(entries: dict[GrainId, DirectoryEntry]) -> None:
            del entries

        async def deactivate(*_args: object) -> None:
            pass

        # Act / Assert
        with pytest.raises(ValueError, match="grace_period"):
            DirectoryRebuildCoordinator(
                local_silo=local,
                transport=tx,
                apply_entries=apply,
                deactivate_duplicate=deactivate,
                grace_period=-1.0,
            )

    def test_rejects_non_positive_rpc_timeout(self) -> None:
        # Arrange
        local = _silos()[0]
        tx = FakeRebuildTransport(local=local)

        async def apply(entries: dict[GrainId, DirectoryEntry]) -> None:
            del entries

        async def deactivate(*_args: object) -> None:
            pass

        # Act / Assert
        with pytest.raises(ValueError, match="rpc_timeout"):
            DirectoryRebuildCoordinator(
                local_silo=local,
                transport=tx,
                apply_entries=apply,
                deactivate_duplicate=deactivate,
                rpc_timeout=0,
            )
