"""Directory rebuild coordinator — reclaim lost ownership after a silo failure.

When a silo dies, the arcs it owned on the consistent hash ring are
reassigned to the next clockwise successor on the new ring. Those
new owners start with empty partitions; without a rebuild, grain
calls that previously resolved to the dead owner's partition would
cascade into duplicate activations on whichever silo first touches
them.

:class:`DirectoryRebuildCoordinator` implements the recovery
protocol from task-02-15: it computes the arcs the local silo
newly owns, broadcasts a REBUILD_QUERY to each remaining peer, and
merges the replies into the directory's ``_owned`` partition —
resolving split-view conflicts by lowest ``activation_epoch`` with
a DEACTIVATE nudge to the losing silo.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from pyleans.cluster.directory import DirectoryEntry
from pyleans.cluster.directory_messages import (
    DIRECTORY_REBUILD_QUERY_HEADER,
    Arc,
    RebuildQueryRequest,
    RebuildQueryResponse,
    decode_rebuild_response,
    encode_rebuild_query,
)
from pyleans.cluster.hash_ring import ConsistentHashRing
from pyleans.identity import GrainId, SiloAddress
from pyleans.transport.cluster import IClusterTransport

logger = logging.getLogger(__name__)

_DEFAULT_GRACE_PERIOD: Final[float] = 2.0
_DEFAULT_RPC_TIMEOUT: Final[float] = 5.0

DirectoryApplyFn = Callable[[dict[GrainId, DirectoryEntry]], Awaitable[None]]
"""Async hook to install merged entries into the directory's ``_owned`` map."""

DeactivateDuplicateFn = Callable[[SiloAddress, GrainId, int], Awaitable[None]]
"""Async hook to send DEACTIVATE to a split-view loser silo."""


class DirectoryRebuildCoordinator:
    """Drives the post-failure rebuild of a directory partition.

    The coordinator is a pure decision+orchestration layer. It does
    not hold directory state itself — the owning
    :class:`DistributedGrainDirectory` supplies apply / deactivate
    hooks. This keeps rebuild testable without a live directory and
    keeps the directory class small.
    """

    def __init__(
        self,
        local_silo: SiloAddress,
        transport: IClusterTransport,
        apply_entries: DirectoryApplyFn,
        deactivate_duplicate: DeactivateDuplicateFn,
        *,
        grace_period: float = _DEFAULT_GRACE_PERIOD,
        rpc_timeout: float = _DEFAULT_RPC_TIMEOUT,
    ) -> None:
        if grace_period < 0:
            raise ValueError(f"grace_period must be >= 0, got {grace_period!r}")
        if rpc_timeout <= 0:
            raise ValueError(f"rpc_timeout must be > 0, got {rpc_timeout!r}")
        self._local = local_silo
        self._transport = transport
        self._apply = apply_entries
        self._deactivate = deactivate_duplicate
        self._grace_period = grace_period
        self._rpc_timeout = rpc_timeout
        self._active_task: asyncio.Task[None] | None = None

    @property
    def is_rebuilding(self) -> bool:
        return self._active_task is not None and not self._active_task.done()

    async def schedule_rebuild(
        self,
        arcs: list[Arc],
        peers: list[SiloAddress],
        *,
        existing_entries: dict[GrainId, DirectoryEntry] | None = None,
    ) -> None:
        """Coalesce rapid membership changes into one rebuild after the grace period.

        If a rebuild is already scheduled, it is cancelled and a new
        one is started with the freshly-computed arcs.
        """
        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._active_task
        self._active_task = asyncio.create_task(
            self._run_rebuild(arcs, peers, dict(existing_entries or {})),
            name="pyleans-directory-rebuild",
        )

    async def cancel(self) -> None:
        if self._active_task is None:
            return
        self._active_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._active_task
        self._active_task = None

    async def _run_rebuild(
        self,
        arcs: list[Arc],
        peers: list[SiloAddress],
        existing_entries: dict[GrainId, DirectoryEntry],
    ) -> None:
        if self._grace_period > 0:
            try:
                await asyncio.sleep(self._grace_period)
            except asyncio.CancelledError:
                return
        if not arcs or not peers:
            logger.debug("rebuild: nothing to do (arcs=%d, peers=%d)", len(arcs), len(peers))
            return
        logger.info(
            "Starting directory rebuild on %s: %d arcs, %d peers",
            self._local.silo_id,
            len(arcs),
            len(peers),
        )
        responses = await asyncio.gather(
            *(self._query_peer(peer, arcs) for peer in peers),
            return_exceptions=True,
        )
        candidates: list[DirectoryEntry] = []
        for peer, raw in zip(peers, responses, strict=True):
            if isinstance(raw, BaseException):
                logger.warning("rebuild query to %s failed: %r", peer.silo_id, raw)
                continue
            candidates.extend(raw.entries)
        merged, duplicates = merge_rebuild_candidates(candidates, existing_entries)
        await self._apply(merged)
        for losing_silo, gid, epoch in duplicates:
            try:
                await self._deactivate(losing_silo, gid, epoch)
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug(
                    "deactivate-duplicate send to %s for %s failed: %r",
                    losing_silo.silo_id,
                    gid,
                    exc,
                )
        logger.info(
            "Rebuild complete on %s: installed %d entries, %d duplicates",
            self._local.silo_id,
            len(merged),
            len(duplicates),
        )

    async def _query_peer(self, peer: SiloAddress, arcs: list[Arc]) -> RebuildQueryResponse:
        body = encode_rebuild_query(RebuildQueryRequest(arcs=arcs))
        _, resp_body = await self._transport.send_request(
            peer, DIRECTORY_REBUILD_QUERY_HEADER, body, timeout=self._rpc_timeout
        )
        return decode_rebuild_response(resp_body)


def compute_new_arcs(
    old_ring: ConsistentHashRing | None,
    new_ring: ConsistentHashRing,
    local_silo: SiloAddress,
) -> list[Arc]:
    """Return the arcs that ``local_silo`` newly owns after the ring change.

    An arc is "newly owned" if the new ring assigns its coverage to
    ``local_silo`` AND (the old ring either did not exist or
    assigned the same coverage to a different silo). Empty arcs
    (zero length) are skipped.
    """
    new_positions = new_ring.positions
    if not new_positions:
        return []
    arcs: list[Arc] = []
    total = len(new_positions)
    for i, node in enumerate(new_positions):
        if node.silo != local_silo:
            continue
        prev_index = (i - 1) % total
        start = new_positions[prev_index].position
        end = node.position
        arc = Arc(start=start, end=end)
        if old_ring is None or _old_ring_owner_of_arc(old_ring, arc) != local_silo:
            arcs.append(arc)
    return arcs


def _old_ring_owner_of_arc(old_ring: ConsistentHashRing, arc: Arc) -> SiloAddress | None:
    """Return the old-ring owner of ``arc`` — the silo owning ``arc.end``."""
    return old_ring.owner_of(arc.end)


def merge_rebuild_candidates(
    candidates: list[DirectoryEntry],
    existing: dict[GrainId, DirectoryEntry] | None = None,
) -> tuple[dict[GrainId, DirectoryEntry], list[tuple[SiloAddress, GrainId, int]]]:
    """Merge query responses into the authoritative partition.

    For each grain, pick the entry with the lowest ``activation_epoch``
    as the winner. Losers (including any pre-existing entry that
    lost) produce an entry in the second tuple value — the
    coordinator sends a DEACTIVATE to each.

    Returns ``(winners_by_grain, duplicates_to_deactivate)``.
    """
    best: dict[GrainId, DirectoryEntry] = {}
    losers: list[tuple[SiloAddress, GrainId, int]] = []

    seed = existing or {}
    for gid, entry in seed.items():
        best[gid] = entry

    for entry in candidates:
        current = best.get(entry.grain_id)
        if current is None:
            best[entry.grain_id] = entry
            continue
        if entry.activation_epoch < current.activation_epoch:
            losers.append((current.silo, current.grain_id, current.activation_epoch))
            best[entry.grain_id] = entry
        elif entry.activation_epoch > current.activation_epoch:
            if entry.silo != current.silo:
                losers.append((entry.silo, entry.grain_id, entry.activation_epoch))
        elif entry.silo != current.silo:
            # Same epoch, different silos — pick lex-smaller silo_id for determinism
            if entry.silo.silo_id < current.silo.silo_id:
                losers.append((current.silo, current.grain_id, current.activation_epoch))
                best[entry.grain_id] = entry
            else:
                losers.append((entry.silo, entry.grain_id, entry.activation_epoch))
    return best, losers
