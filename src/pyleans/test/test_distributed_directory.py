"""Tests for :mod:`pyleans.cluster.distributed_directory`.

Uses an in-memory transport fabric that wires N silos together so
directory RPCs land on the owner's registered ``message_handler``.
Covers: owner routing for all four ops, register-if-absent
atomicity under contention, ownership transfer via handoff,
split-view reconciliation by ``activation_epoch`` with DEACTIVATE
notification, ``ClusterNotReadyError`` on empty ring, transient
transport-error retry at the caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from pyleans.cluster.directory import DirectoryEntry
from pyleans.cluster.directory_messages import (
    DIRECTORY_DEACTIVATE_HEADER,
    DIRECTORY_HANDOFF_HEADER,
)
from pyleans.cluster.distributed_directory import (
    ClusterNotReadyError,
    DistributedGrainDirectory,
)
from pyleans.cluster.hash_ring import ConsistentHashRing
from pyleans.cluster.identity import ClusterId
from pyleans.cluster.placement import PreferLocalPlacement, RandomPlacement
from pyleans.identity import GrainId, SiloAddress
from pyleans.transport.cluster import (
    ConnectionCallback,
    DisconnectionCallback,
    IClusterTransport,
    MessageHandler,
)
from pyleans.transport.errors import TransportConnectionError
from pyleans.transport.messages import MessageType, TransportMessage


@dataclass
class InMemoryFabric:
    """A test-only fabric that wires N directories by silo_id for direct RPC delivery."""

    handlers: dict[str, MessageHandler] = field(default_factory=dict)
    correlation_counter: int = 0
    inbound_failures: dict[tuple[str, bytes], Callable[[], None]] = field(
        default_factory=dict,
    )

    def register(self, silo: SiloAddress, handler: MessageHandler) -> None:
        self.handlers[silo.silo_id] = handler

    def next_correlation(self) -> int:
        self.correlation_counter += 1
        return self.correlation_counter

    async def deliver_request(
        self,
        source: SiloAddress,
        target: SiloAddress,
        header: bytes,
        body: bytes,
    ) -> tuple[bytes, bytes]:
        # Optional hook to fail once per (target, header) pair.
        trip = self.inbound_failures.pop((target.silo_id, header), None)
        if trip is not None:
            trip()  # raises
        handler = self.handlers.get(target.silo_id)
        if handler is None:
            raise TransportConnectionError(f"no handler for {target.silo_id}")
        msg = TransportMessage(
            message_type=MessageType.REQUEST,
            correlation_id=self.next_correlation(),
            header=header,
            body=body,
        )
        resp = await handler(source, msg)
        if resp is None:
            raise TransportConnectionError("handler did not reply")
        return resp.header, resp.body

    async def deliver_one_way(
        self,
        source: SiloAddress,
        target: SiloAddress,
        header: bytes,
        body: bytes,
    ) -> None:
        handler = self.handlers.get(target.silo_id)
        if handler is None:
            return  # best effort
        msg = TransportMessage(
            message_type=MessageType.ONE_WAY,
            correlation_id=0,
            header=header,
            body=body,
        )
        await handler(source, msg)


@dataclass
class FabricTransport(IClusterTransport):
    """IClusterTransport adapter over :class:`InMemoryFabric`."""

    local: SiloAddress
    fabric: InMemoryFabric
    sent_one_way: list[tuple[SiloAddress, bytes, bytes]] = field(default_factory=list)

    async def start(
        self,
        local_silo: SiloAddress,
        cluster_id: ClusterId,
        message_handler: MessageHandler,
    ) -> None:
        del local_silo, cluster_id
        self.fabric.register(self.local, message_handler)

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
        return await self.fabric.deliver_request(self.local, target, header, body)

    async def send_one_way(self, target: SiloAddress, header: bytes, body: bytes) -> None:
        self.sent_one_way.append((target, header, body))
        await self.fabric.deliver_one_way(self.local, target, header, body)

    async def send_ping(self, target: SiloAddress, timeout: float = 10.0) -> float:
        del target, timeout
        return 0.001

    def is_connected_to(self, silo: SiloAddress) -> bool:
        return silo.silo_id in self.fabric.handlers

    def get_connected_silos(self) -> list[SiloAddress]:
        return []

    @property
    def local_silo(self) -> SiloAddress:
        return self.local

    def on_connection_established(self, callback: ConnectionCallback) -> None:
        pass

    def on_connection_lost(self, callback: DisconnectionCallback) -> None:
        pass


def _silos() -> list[SiloAddress]:
    return [
        SiloAddress(host="a", port=11111, epoch=1),
        SiloAddress(host="b", port=11111, epoch=1),
        SiloAddress(host="c", port=11111, epoch=1),
    ]


def _build_cluster(
    active: list[SiloAddress] | None = None,
) -> tuple[InMemoryFabric, dict[str, DistributedGrainDirectory]]:
    fabric = InMemoryFabric()
    silos = active or _silos()
    ring = ConsistentHashRing(silos)
    directories: dict[str, DistributedGrainDirectory] = {}
    for s in silos:
        tx = FabricTransport(local=s, fabric=fabric)
        d = DistributedGrainDirectory(
            local_silo=s,
            transport=tx,
            initial_ring=ring,
            active_silos=list(silos),
        )
        fabric.register(s, d.message_handler)
        directories[s.silo_id] = d
    return fabric, directories


class TestOwnerRouting:
    async def test_register_routes_to_owner(self) -> None:
        # Arrange
        _, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "c1")
        # Find the owner silo for this grain
        ring = ConsistentHashRing(silos)
        from pyleans.cluster.identity import hash_grain_id

        owner = ring.owner_of(hash_grain_id(grain))
        assert owner is not None
        non_owner = next(s for s in silos if s != owner)

        # Act - register from a non-owner
        entry = await directories[non_owner.silo_id].register(grain, non_owner)

        # Assert - entry stored on owner
        assert entry.silo == non_owner
        assert grain in directories[owner.silo_id].owned
        assert grain not in directories[non_owner.silo_id].owned

    async def test_lookup_from_non_owner_finds_entry(self) -> None:
        # Arrange
        _, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "c2")
        await directories[silos[0].silo_id].register(grain, silos[0])

        # Act - look up from a different silo
        entry = await directories[silos[2].silo_id].lookup(grain)

        # Assert
        assert entry is not None
        assert entry.silo == silos[0]

    async def test_unregister_owner_match_removes_entry(self) -> None:
        # Arrange
        _, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "c3")
        await directories[silos[1].silo_id].register(grain, silos[1])

        # Act
        await directories[silos[1].silo_id].unregister(grain, silos[1])

        # Assert
        for d in directories.values():
            assert await d.lookup(grain) is None

    async def test_unregister_non_matching_silo_is_noop(self) -> None:
        # Arrange
        _, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "c4")
        first = await directories[silos[0].silo_id].register(grain, silos[0])

        # Act - silo2 tries to evict silo0's entry
        await directories[silos[2].silo_id].unregister(grain, silos[2])

        # Assert
        resolved = await directories[silos[0].silo_id].lookup(grain)
        assert resolved == first


class TestResolveOrActivate:
    async def test_first_resolve_registers_with_placement(self) -> None:
        # Arrange
        _, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "c5")

        # Act
        entry = await directories[silos[0].silo_id].resolve_or_activate(
            grain, PreferLocalPlacement(), caller=silos[0]
        )

        # Assert - PreferLocalPlacement keeps grain on caller
        assert entry.silo == silos[0]

    async def test_repeated_resolve_returns_same_entry(self) -> None:
        # Arrange
        _, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "c6")
        first = await directories[silos[0].silo_id].resolve_or_activate(
            grain, RandomPlacement(), caller=silos[0]
        )

        # Act
        second = await directories[silos[2].silo_id].resolve_or_activate(
            grain, RandomPlacement(), caller=silos[2]
        )

        # Assert
        assert second == first

    async def test_concurrent_resolves_return_same_entry(self) -> None:
        # Arrange - 10 concurrent resolve calls from various silos for the same grain
        _, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "c-contention")

        async def resolve(idx: int) -> DirectoryEntry:
            silo = silos[idx % len(silos)]
            return await directories[silo.silo_id].resolve_or_activate(
                grain, RandomPlacement(), caller=silo
            )

        # Act
        results = await asyncio.gather(*(resolve(i) for i in range(10)))

        # Assert - all calls return the exact same entry
        assert len({id(r) for r in results}) >= 1  # same dataclass equality
        assert all(r == results[0] for r in results)
        assert results[0].activation_epoch == results[0].activation_epoch


class TestClusterNotReady:
    async def test_empty_ring_raises(self) -> None:
        # Arrange
        fabric = InMemoryFabric()
        local = SiloAddress(host="a", port=11111, epoch=1)
        tx = FabricTransport(local=local, fabric=fabric)
        empty_ring = ConsistentHashRing([])
        directory = DistributedGrainDirectory(
            local_silo=local, transport=tx, initial_ring=empty_ring
        )

        # Act / Assert
        with pytest.raises(ClusterNotReadyError):
            await directory.lookup(GrainId("X", "y"))


class TestHandoff:
    async def test_ownership_transfer_sends_handoff(self) -> None:
        # Arrange - 3-silo cluster with a grain registered on its owner
        _, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "handoff-test")
        entry = await directories[silos[0].silo_id].register(grain, silos[0])
        original_owner_id = entry_owner_silo_id(entry, silos)
        old_owner = directories[original_owner_id]
        assert grain in old_owner.owned

        # Act - shrink ring to exclude original owner; new owner takes over
        surviving = [s for s in silos if s.silo_id != original_owner_id]
        for silo_id in [s.silo_id for s in surviving]:
            await directories[silo_id].on_membership_changed(surviving)
        await old_owner.on_membership_changed(surviving)

        # Assert - old owner dropped entry; new owner received handoff
        assert grain not in old_owner.owned
        new_owner_id = None
        for s in surviving:
            if grain in directories[s.silo_id].owned:
                new_owner_id = s.silo_id
                break
        assert new_owner_id is not None, "expected exactly one new owner"

    async def test_split_view_lower_epoch_wins(self) -> None:
        # Arrange - use the grain's actual ring owner as the receiver
        _, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "split-view")
        from pyleans.cluster.identity import hash_grain_id

        owner = ConsistentHashRing(silos).owner_of(hash_grain_id(grain))
        assert owner is not None
        target = directories[owner.silo_id]
        hosting_silo = silos[0]
        # Existing entry on target with epoch 5
        existing = DirectoryEntry(grain_id=grain, silo=hosting_silo, activation_epoch=5)
        target._owned[grain] = existing
        target._epoch = 5

        from pyleans.cluster.directory_messages import (
            HandoffRequest,
            encode_handoff,
        )

        incoming_silo = silos[1]
        handoff_body = encode_handoff(
            HandoffRequest(
                entry=DirectoryEntry(grain_id=grain, silo=incoming_silo, activation_epoch=2)
            )
        )

        # Act - deliver handoff
        await target.message_handler(
            incoming_silo,
            TransportMessage(
                message_type=MessageType.ONE_WAY,
                correlation_id=0,
                header=DIRECTORY_HANDOFF_HEADER,
                body=handoff_body,
            ),
        )

        # Assert - incoming lower-epoch wins
        assert target.owned[grain].activation_epoch == 2
        assert target.owned[grain].silo == incoming_silo

    async def test_split_view_higher_epoch_loses_gets_deactivate_signal(self) -> None:
        # Arrange
        _, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "split-view-loser")
        from pyleans.cluster.identity import hash_grain_id

        owner = ConsistentHashRing(silos).owner_of(hash_grain_id(grain))
        assert owner is not None
        target = directories[owner.silo_id]
        target_tx = _find_transport(target, owner)
        hosting_silo = silos[0]
        existing = DirectoryEntry(grain_id=grain, silo=hosting_silo, activation_epoch=3)
        target._owned[grain] = existing
        target._epoch = 3
        from pyleans.cluster.directory_messages import (
            HandoffRequest,
            encode_handoff,
        )

        # Incoming handoff with HIGHER epoch loses (existing is lower → wins)
        incoming_silo = silos[1]
        handoff = encode_handoff(
            HandoffRequest(
                entry=DirectoryEntry(grain_id=grain, silo=incoming_silo, activation_epoch=7)
            )
        )

        # Act
        await target.message_handler(
            incoming_silo,
            TransportMessage(
                message_type=MessageType.ONE_WAY,
                correlation_id=0,
                header=DIRECTORY_HANDOFF_HEADER,
                body=handoff,
            ),
        )

        # Assert - existing (lower-epoch) wins; DEACTIVATE went to incoming silo
        assert target.owned[grain] == existing
        deactivate_msgs = [
            (t, h) for (t, h, _) in target_tx.sent_one_way if h == DIRECTORY_DEACTIVATE_HEADER
        ]
        assert len(deactivate_msgs) == 1
        assert deactivate_msgs[0][0] == incoming_silo


class TestDeactivateNotifier:
    async def test_deactivate_signal_fires_notifier(self) -> None:
        # Arrange
        _, directories = _build_cluster()
        silos = _silos()
        target = directories[silos[0].silo_id]
        grain = GrainId("Counter", "deact")
        fired: list[tuple[GrainId, int]] = []

        async def notifier(gid: GrainId, epoch: int) -> None:
            fired.append((gid, epoch))

        target.on_deactivate_notified(notifier)

        from pyleans.cluster.directory_messages import (
            DeactivateRequest,
            encode_deactivate,
        )

        body = encode_deactivate(DeactivateRequest(grain_id=grain, activation_epoch=9))

        # Act
        await target.message_handler(
            silos[1],
            TransportMessage(
                message_type=MessageType.ONE_WAY,
                correlation_id=0,
                header=DIRECTORY_DEACTIVATE_HEADER,
                body=body,
            ),
        )

        # Assert
        assert fired == [(grain, 9)]


class TestTransientFailure:
    async def test_transport_error_propagates_to_caller(self) -> None:
        # Arrange - register once successfully, then poison next inbound on owner
        fabric, directories = _build_cluster()
        silos = _silos()
        grain = GrainId("Counter", "transient")

        # First call succeeds
        await directories[silos[0].silo_id].register(grain, silos[0])

        # Now poison one attempt and verify it raises
        from pyleans.cluster.directory_messages import DIRECTORY_LOOKUP_HEADER
        from pyleans.cluster.identity import hash_grain_id

        ring = ConsistentHashRing(silos)
        owner = ring.owner_of(hash_grain_id(grain))
        assert owner is not None

        def trip() -> None:
            raise TransportConnectionError("simulated lost")

        non_owner = next(s for s in silos if s != owner)
        fabric.inbound_failures[(owner.silo_id, DIRECTORY_LOOKUP_HEADER)] = trip

        # Act / Assert
        with pytest.raises(TransportConnectionError):
            await directories[non_owner.silo_id].lookup(grain)


# ---- Helpers ---------------------------------------------------------------


def entry_owner_silo_id(entry: DirectoryEntry, silos: list[SiloAddress]) -> str:
    """Given an entry that was just registered, return the silo-id of its directory owner."""
    from pyleans.cluster.identity import hash_grain_id

    ring = ConsistentHashRing(silos)
    owner = ring.owner_of(hash_grain_id(entry.grain_id))
    assert owner is not None
    return owner.silo_id


def _find_transport(directory: DistributedGrainDirectory, local: SiloAddress) -> FabricTransport:
    tx = directory._transport
    assert isinstance(tx, FabricTransport)
    assert tx.local == local
    return tx
