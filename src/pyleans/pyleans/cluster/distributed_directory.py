"""Distributed grain directory — partitioned by consistent hash ring.

Every :class:`GrainId` has exactly one **owner silo** — the silo whose
virtual node is the first clockwise successor of
``hash_grain_id(grain_id)`` on the ring. The owner holds the
authoritative :class:`DirectoryEntry`; every other silo consults the
owner via a directory RPC.

This is the enforcement point for the single-activation contract
from :doc:`../../../docs/adr/adr-single-activation-cluster`: the
owner's single-threaded asyncio task funnels all directory mutations
for a given ``GrainId`` serially, so 100 concurrent
``resolve_or_activate`` calls for the same grain return the same
entry.

The directory is explicitly **eventually consistent** under membership
change; split-view reconciliation uses the lower-``activation_epoch``
winner with a ``DEACTIVATE`` nudge to the losing silo. See
:doc:`../../../docs/adr/adr-grain-directory`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from pyleans.cluster.directory import DirectoryEntry, IGrainDirectory
from pyleans.cluster.directory_messages import (
    DIRECTORY_DEACTIVATE_HEADER,
    DIRECTORY_HANDOFF_HEADER,
    DIRECTORY_LOOKUP_HEADER,
    DIRECTORY_REGISTER_HEADER,
    DIRECTORY_RESOLVE_HEADER,
    DIRECTORY_UNREGISTER_HEADER,
    DeactivateRequest,
    HandoffRequest,
    LookupRequest,
    RegisterRequest,
    ResolveRequest,
    UnregisterRequest,
    decode_deactivate,
    decode_entry,
    decode_handoff,
    decode_lookup,
    decode_none,
    decode_register,
    decode_resolve,
    decode_unregister,
    encode_deactivate,
    encode_entry,
    encode_handoff,
    encode_lookup,
    encode_none,
    encode_register,
    encode_resolve,
    encode_unregister,
    is_directory_header,
    placement_class_name,
)
from pyleans.cluster.hash_ring import ConsistentHashRing
from pyleans.cluster.identity import hash_grain_id
from pyleans.cluster.placement import (
    PlacementStrategy,
    PreferLocalPlacement,
    RandomPlacement,
)
from pyleans.errors import PyleansError
from pyleans.identity import GrainId, SiloAddress
from pyleans.transport.cluster import IClusterTransport
from pyleans.transport.errors import TransportError
from pyleans.transport.messages import MessageType, TransportMessage

logger = logging.getLogger(__name__)

_RPC_TIMEOUT: Final[float] = 5.0
_PLACEMENT_CLASSES: Final[dict[str, type[PlacementStrategy]]] = {
    "RandomPlacement": RandomPlacement,
    "PreferLocalPlacement": PreferLocalPlacement,
}

DeactivateNotifier = Callable[[GrainId, int], Awaitable[None]]
"""Fired on the losing silo during split-view reconciliation.

The agent notifies the runtime so it can deactivate the losing
activation; the directory itself cannot reach into the runtime.
"""


class ClusterNotReadyError(PyleansError):
    """The hash ring is empty — no silo can own any grain."""


class DistributedGrainDirectory(IGrainDirectory):
    """Owner-partitioned :class:`IGrainDirectory` over the cluster transport.

    Ownership is deterministic from the ring; callers re-route when
    they need to talk to a peer. The silo that installs this
    directory must also wire :meth:`message_handler` into its
    transport dispatcher.
    """

    def __init__(
        self,
        local_silo: SiloAddress,
        transport: IClusterTransport,
        initial_ring: ConsistentHashRing,
        *,
        active_silos: list[SiloAddress] | None = None,
        rpc_timeout: float = _RPC_TIMEOUT,
    ) -> None:
        self._local = local_silo
        self._transport = transport
        self._ring = initial_ring
        self._active_silos: list[SiloAddress] = list(active_silos) if active_silos else []
        self._rpc_timeout = rpc_timeout
        self._owned: dict[GrainId, DirectoryEntry] = {}
        self._epoch = 0
        self._lock = asyncio.Lock()
        self._deactivate_notifier: DeactivateNotifier | None = None

    @property
    def local_silo(self) -> SiloAddress:
        return self._local

    @property
    def ring(self) -> ConsistentHashRing:
        return self._ring

    @property
    def owned(self) -> dict[GrainId, DirectoryEntry]:
        return dict(self._owned)

    def on_deactivate_notified(self, notifier: DeactivateNotifier) -> None:
        """Install the hook the directory calls when a split-view loser must stand down."""
        self._deactivate_notifier = notifier

    async def on_membership_changed(self, active: list[SiloAddress]) -> None:
        """Rebuild the ring and stream handoffs for entries that moved off-silo."""
        async with self._lock:
            self._active_silos = list(active)
            old_ring = self._ring
            new_ring = ConsistentHashRing(active) if active else self._ring
            self._ring = new_ring
            handoffs: list[tuple[SiloAddress, DirectoryEntry]] = []
            stale_ids: list[GrainId] = []
            for grain_id, entry in list(self._owned.items()):
                new_owner = self._owner_of_with_ring(grain_id, new_ring)
                if new_owner != self._local:
                    handoffs.append((new_owner, entry))
                    stale_ids.append(grain_id)
            for gid in stale_ids:
                self._owned.pop(gid, None)
            del old_ring
        for new_owner, entry in handoffs:
            await self._send_handoff(new_owner, entry)

    async def register(self, grain_id: GrainId, silo: SiloAddress) -> DirectoryEntry:
        owner = self._owner_of(grain_id)
        if owner == self._local:
            return await self._register_local(grain_id, silo)
        return await self._register_remote(owner, grain_id, silo)

    async def lookup(self, grain_id: GrainId) -> DirectoryEntry | None:
        owner = self._owner_of(grain_id)
        if owner == self._local:
            return self._owned.get(grain_id)
        return await self._lookup_remote(owner, grain_id)

    async def unregister(self, grain_id: GrainId, silo: SiloAddress) -> None:
        owner = self._owner_of(grain_id)
        if owner == self._local:
            await self._unregister_local(grain_id, silo)
            return
        await self._unregister_remote(owner, grain_id, silo)

    async def resolve_or_activate(
        self,
        grain_id: GrainId,
        placement: PlacementStrategy,
        caller: SiloAddress | None,
    ) -> DirectoryEntry:
        owner = self._owner_of(grain_id)
        if owner == self._local:
            return await self._resolve_or_activate_local(grain_id, placement, caller)
        return await self._resolve_or_activate_remote(owner, grain_id, placement, caller)

    # ---- Local-owner paths ---------------------------------------------------

    async def _register_local(self, grain_id: GrainId, silo: SiloAddress) -> DirectoryEntry:
        async with self._lock:
            existing = self._owned.get(grain_id)
            if existing is not None:
                return existing
            self._epoch += 1
            entry = DirectoryEntry(grain_id=grain_id, silo=silo, activation_epoch=self._epoch)
            self._owned[grain_id] = entry
            return entry

    async def _unregister_local(self, grain_id: GrainId, silo: SiloAddress) -> None:
        async with self._lock:
            entry = self._owned.get(grain_id)
            if entry is None:
                return
            if entry.silo != silo:
                logger.debug(
                    "Owner %s ignoring unregister of %s by %s: owner is %s",
                    self._local.silo_id,
                    grain_id,
                    silo.silo_id,
                    entry.silo.silo_id,
                )
                return
            del self._owned[grain_id]

    async def _resolve_or_activate_local(
        self,
        grain_id: GrainId,
        placement: PlacementStrategy,
        caller: SiloAddress | None,
    ) -> DirectoryEntry:
        async with self._lock:
            existing = self._owned.get(grain_id)
            if existing is not None:
                return existing
            active = self._placement_candidates()
            if not active:
                raise ClusterNotReadyError(
                    f"cannot place {grain_id}: active silo set is empty",
                )
            chosen = placement.pick_silo(grain_id, caller, active)
            self._epoch += 1
            entry = DirectoryEntry(grain_id=grain_id, silo=chosen, activation_epoch=self._epoch)
            self._owned[grain_id] = entry
            return entry

    def _placement_candidates(self) -> list[SiloAddress]:
        if self._active_silos:
            return list(self._active_silos)
        return list(self._ring.silos)

    # ---- Remote-owner paths --------------------------------------------------

    async def _register_remote(
        self, owner: SiloAddress, grain_id: GrainId, silo: SiloAddress
    ) -> DirectoryEntry:
        body = encode_register(RegisterRequest(grain_id=grain_id, silo=silo))
        _, resp_body = await self._send_rpc(owner, DIRECTORY_REGISTER_HEADER, body)
        entry = decode_entry(resp_body)
        if entry is None:
            raise TransportError("register RPC returned null entry")
        return entry

    async def _lookup_remote(self, owner: SiloAddress, grain_id: GrainId) -> DirectoryEntry | None:
        body = encode_lookup(LookupRequest(grain_id=grain_id))
        _, resp_body = await self._send_rpc(owner, DIRECTORY_LOOKUP_HEADER, body)
        return decode_entry(resp_body)

    async def _unregister_remote(
        self, owner: SiloAddress, grain_id: GrainId, silo: SiloAddress
    ) -> None:
        body = encode_unregister(UnregisterRequest(grain_id=grain_id, silo=silo))
        _, resp_body = await self._send_rpc(owner, DIRECTORY_UNREGISTER_HEADER, body)
        decode_none(resp_body)

    async def _resolve_or_activate_remote(
        self,
        owner: SiloAddress,
        grain_id: GrainId,
        placement: PlacementStrategy,
        caller: SiloAddress | None,
    ) -> DirectoryEntry:
        cls_name = placement_class_name(placement)
        if cls_name not in _PLACEMENT_CLASSES:
            raise ValueError(
                f"placement strategy {cls_name!r} is not recognised by the directory",
            )
        body = encode_resolve(
            ResolveRequest(grain_id=grain_id, placement_class=cls_name, caller=caller)
        )
        _, resp_body = await self._send_rpc(owner, DIRECTORY_RESOLVE_HEADER, body)
        entry = decode_entry(resp_body)
        if entry is None:
            raise TransportError("resolve_or_activate RPC returned null entry")
        return entry

    async def _send_rpc(
        self, owner: SiloAddress, header: bytes, body: bytes
    ) -> tuple[bytes, bytes]:
        try:
            return await self._transport.send_request(
                owner, header, body, timeout=self._rpc_timeout
            )
        except TransportError:
            logger.debug(
                "directory rpc %s to %s raised; surface to caller",
                header,
                owner.silo_id,
            )
            raise

    async def _send_handoff(self, new_owner: SiloAddress, entry: DirectoryEntry) -> None:
        try:
            await self._transport.send_one_way(
                new_owner,
                DIRECTORY_HANDOFF_HEADER,
                encode_handoff(HandoffRequest(entry=entry)),
            )
        except TransportError as exc:
            logger.warning(
                "Directory handoff of %s to %s failed: %s",
                entry.grain_id,
                new_owner.silo_id,
                exc,
            )

    async def _notify_deactivate(
        self, loser_silo: SiloAddress, grain_id: GrainId, activation_epoch: int
    ) -> None:
        body = encode_deactivate(
            DeactivateRequest(grain_id=grain_id, activation_epoch=activation_epoch)
        )
        try:
            await self._transport.send_one_way(loser_silo, DIRECTORY_DEACTIVATE_HEADER, body)
        except TransportError as exc:
            logger.warning(
                "Directory deactivate notify to %s for %s failed: %s",
                loser_silo.silo_id,
                grain_id,
                exc,
            )

    # ---- Inbound request handling -------------------------------------------

    async def message_handler(
        self, source: SiloAddress, msg: TransportMessage
    ) -> TransportMessage | None:
        """Dispatcher for the six directory wire headers."""
        if not is_directory_header(msg.header):
            return None
        header = msg.header
        if msg.message_type == MessageType.REQUEST:
            return await self._handle_request(source, header, msg)
        if msg.message_type == MessageType.ONE_WAY:
            await self._handle_one_way(source, header, msg)
            return None
        return None

    async def _handle_request(
        self, source: SiloAddress, header: bytes, msg: TransportMessage
    ) -> TransportMessage | None:
        del source
        try:
            if header == DIRECTORY_REGISTER_HEADER:
                req = decode_register(msg.body)
                entry = await self._register_local(req.grain_id, req.silo)
                body = encode_entry(entry)
            elif header == DIRECTORY_LOOKUP_HEADER:
                lreq = decode_lookup(msg.body)
                body = encode_entry(self._owned.get(lreq.grain_id))
            elif header == DIRECTORY_UNREGISTER_HEADER:
                ureq = decode_unregister(msg.body)
                await self._unregister_local(ureq.grain_id, ureq.silo)
                body = encode_none()
            elif header == DIRECTORY_RESOLVE_HEADER:
                rreq = decode_resolve(msg.body)
                placement_cls = _PLACEMENT_CLASSES.get(rreq.placement_class)
                if placement_cls is None:
                    raise ValueError(
                        f"unknown placement strategy {rreq.placement_class!r}",
                    )
                placement = placement_cls()
                entry = await self._resolve_or_activate_local(rreq.grain_id, placement, rreq.caller)
                body = encode_entry(entry)
            else:
                return None
        except ValueError as exc:
            logger.warning("Malformed directory RPC %s: %s", header, exc)
            return None
        return TransportMessage(
            message_type=MessageType.RESPONSE,
            correlation_id=msg.correlation_id,
            header=header,
            body=body,
        )

    async def _handle_one_way(
        self, source: SiloAddress, header: bytes, msg: TransportMessage
    ) -> None:
        try:
            if header == DIRECTORY_HANDOFF_HEADER:
                handoff = decode_handoff(msg.body)
                await self._apply_handoff(source, handoff)
            elif header == DIRECTORY_DEACTIVATE_HEADER:
                dreq = decode_deactivate(msg.body)
                await self._apply_deactivate(dreq)
        except ValueError as exc:
            logger.warning(
                "Malformed directory one-way %s from %s: %s",
                header,
                source.silo_id,
                exc,
            )

    async def _apply_handoff(self, source: SiloAddress, handoff: HandoffRequest) -> None:
        incoming = handoff.entry
        grain_id = incoming.grain_id
        new_owner_is_local = self._owner_of(grain_id) == self._local
        if not new_owner_is_local:
            logger.debug(
                "Ignoring handoff of %s from %s: I am not the new owner",
                grain_id,
                source.silo_id,
            )
            return
        async with self._lock:
            existing = self._owned.get(grain_id)
            if existing is None:
                self._owned[grain_id] = incoming
                self._epoch = max(self._epoch, incoming.activation_epoch)
                return
            if existing.activation_epoch <= incoming.activation_epoch:
                # Keep lower-epoch winner (existing); tell incoming silo to stand down
                loser_entry = incoming
            else:
                loser_entry = existing
                self._owned[grain_id] = incoming
                self._epoch = max(self._epoch, incoming.activation_epoch)
        if loser_entry is not None:
            await self._notify_deactivate(loser_entry.silo, grain_id, loser_entry.activation_epoch)

    async def _apply_deactivate(self, req: DeactivateRequest) -> None:
        if self._deactivate_notifier is None:
            logger.debug("Received DEACTIVATE for %s but no notifier installed", req.grain_id)
            return
        try:
            await self._deactivate_notifier(req.grain_id, req.activation_epoch)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error(
                "Deactivate notifier raised for %s: %s",
                req.grain_id,
                exc,
                exc_info=True,
            )

    # ---- Helpers -------------------------------------------------------------

    def _owner_of(self, grain_id: GrainId) -> SiloAddress:
        return self._owner_of_with_ring(grain_id, self._ring)

    def _owner_of_with_ring(self, grain_id: GrainId, ring: ConsistentHashRing) -> SiloAddress:
        owner = ring.owner_of(hash_grain_id(grain_id))
        if owner is None:
            raise ClusterNotReadyError(
                f"cannot resolve owner for {grain_id}: ring is empty",
            )
        return owner
