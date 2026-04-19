"""MembershipAgent — one per silo, drives the failure detector's event loops.

Composition:

* Owns a :class:`FailureDetector` (pure decisions) and schedules three
  asyncio tasks — probe loop, table-poll loop, IAmAlive loop — that
  feed it inputs and service its outputs.
* Exposes :meth:`message_handler` as the transport-side dispatcher for
  the two inter-silo messages the detector relies on:
  ``INDIRECT_PROBE`` (request/response) and ``MEMBERSHIP_SNAPSHOT``
  (one-way broadcast).
* Fires a self-terminate hook when the agent observes its own row at
  ``status=DEAD`` — the rest of the cluster has already decided this
  silo is gone (Orleans §3.7, "enforcing perfect failure detection").

Orchestration only — no per-peer health state lives here; it lives on
the detector.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from pyleans.cluster.failure_detector import (
    INDIRECT_PROBE_HEADER,
    FailureDetector,
    ProbeResult,
)
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus, SuspicionVote
from pyleans.providers.errors import (
    MembershipUnavailableError,
    TableStaleError,
)
from pyleans.providers.membership import (
    MembershipProvider,
    MembershipSnapshot,
    with_replacements,
)
from pyleans.transport.cluster import IClusterTransport
from pyleans.transport.messages import MessageType, TransportMessage

logger = logging.getLogger(__name__)

MEMBERSHIP_SNAPSHOT_HEADER: Final[bytes] = b"pyleans/membership-snapshot/v1"
"""Wire-header tag identifying a ``MEMBERSHIP_SNAPSHOT`` one-way broadcast."""

SelfTerminateHook = Callable[[], Awaitable[None]]
SnapshotReceivedHook = Callable[[MembershipSnapshot], Awaitable[None]]


class MembershipAgent:  # pylint: disable=too-many-instance-attributes
    """One-per-silo agent that runs the failure detector's loops.

    The agent does not hold peer health — that lives on the
    :class:`FailureDetector`. The agent owns: three asyncio tasks,
    the latest snapshot cache, the transport message dispatcher for
    INDIRECT_PROBE / MEMBERSHIP_SNAPSHOT, and two hooks the silo
    wires up (self-terminate, snapshot-received).
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        local_silo: SiloAddress,
        detector: FailureDetector,
        transport: IClusterTransport,
        membership: MembershipProvider,
        *,
        wall_clock: Callable[[], float] = time.time,
        loop_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._local = local_silo
        self._detector = detector
        self._options = detector.options
        self._transport = transport
        self._membership = membership
        self._wall_clock = wall_clock
        self._loop_clock = loop_clock
        self._probe_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._alive_task: asyncio.Task[None] | None = None
        self._running = False
        self._self_terminate_hook: SelfTerminateHook | None = None
        self._snapshot_hook: SnapshotReceivedHook | None = None
        self._last_snapshot: MembershipSnapshot | None = None

    @property
    def detector(self) -> FailureDetector:
        return self._detector

    @property
    def last_snapshot(self) -> MembershipSnapshot | None:
        return self._last_snapshot

    @property
    def is_running(self) -> bool:
        return self._running

    def on_self_terminate(self, hook: SelfTerminateHook) -> None:
        """Install the hook invoked when the local row is observed as DEAD."""
        self._self_terminate_hook = hook

    def on_snapshot_received(self, hook: SnapshotReceivedHook) -> None:
        """Install the hook invoked after the agent accepts a broadcast snapshot."""
        self._snapshot_hook = hook

    async def start(self) -> None:
        if self._running:
            raise RuntimeError("MembershipAgent already started")
        self._running = True
        self._transport.on_connection_lost(self._on_peer_disconnect)
        self._probe_task = asyncio.create_task(self._probe_loop(), name="pyleans-probe")
        self._poll_task = asyncio.create_task(self._table_poll_loop(), name="pyleans-table-poll")
        self._alive_task = asyncio.create_task(self._i_am_alive_loop(), name="pyleans-i-am-alive")

    async def stop(self) -> None:
        self._running = False
        tasks = [t for t in (self._probe_task, self._poll_task, self._alive_task) if t]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug("Agent task cleanup raised: %r", exc)
        self._probe_task = self._poll_task = self._alive_task = None

    async def message_handler(
        self, source: SiloAddress, msg: TransportMessage
    ) -> TransportMessage | None:
        """Dispatcher for the two agent-owned wire messages; None otherwise."""
        if msg.header == INDIRECT_PROBE_HEADER and msg.message_type == MessageType.REQUEST:
            body = await self._detector.handle_indirect_probe_request(msg.body)
            return TransportMessage(
                message_type=MessageType.RESPONSE,
                correlation_id=msg.correlation_id,
                header=INDIRECT_PROBE_HEADER,
                body=body,
            )
        if msg.header == MEMBERSHIP_SNAPSHOT_HEADER and msg.message_type == MessageType.ONE_WAY:
            try:
                snapshot = decode_snapshot(msg.body)
            except ValueError as exc:
                logger.warning("Malformed snapshot from %s: %s", source.silo_id, exc)
                return None
            await self._apply_snapshot(snapshot, source=source)
            return None
        return None

    async def _probe_loop(self) -> None:
        while self._running:
            try:
                await self._do_probe_cycle()
            except Exception as exc:  # pylint: disable=broad-except
                # CancelledError is a BaseException in 3.12+ and bypasses
                # ``except Exception`` naturally; no explicit re-raise needed.
                logger.error("Probe cycle raised %r", exc, exc_info=True)
            if not self._running:
                return
            await self._sleep_with_drift(self._options.probe_timeout)

    async def _do_probe_cycle(self) -> None:
        snapshot = await self._safe_read_all()
        if snapshot is None:
            return
        self._last_snapshot = snapshot
        if self._own_row_is_dead(snapshot):
            await self._self_terminate()
            return
        targets = self._detector.probe_targets(snapshot)
        if not targets:
            return
        results = await asyncio.gather(
            *(self._detector.probe_once(peer) for peer in targets),
            return_exceptions=True,
        )
        any_declared = False
        for peer, raw in zip(targets, results, strict=True):
            if isinstance(raw, BaseException):
                logger.warning("Probe to %s raised %r", peer.silo_id, raw)
                result = ProbeResult(ok=False, reason=f"exception: {raw!r}")
            else:
                result = raw
            declared = await self._detector.on_probe_result(peer, result, snapshot)
            any_declared = any_declared or declared
        if any_declared:
            await self._broadcast_snapshot()

    async def _table_poll_loop(self) -> None:
        while self._running:
            snapshot = await self._safe_read_all()
            if snapshot is not None:
                self._last_snapshot = snapshot
                if self._own_row_is_dead(snapshot):
                    await self._self_terminate()
                    return
            if not self._running:
                return
            await asyncio.sleep(self._options.table_poll_interval)

    async def _i_am_alive_loop(self) -> None:
        while self._running:
            try:
                await self._write_i_am_alive()
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("i_am_alive write failed: %s", exc, exc_info=True)
            if not self._running:
                return
            await asyncio.sleep(self._options.i_am_alive_interval)

    async def _write_i_am_alive(self) -> None:
        try:
            own_row = await self._membership.read_silo(self._local.silo_id)
        except MembershipUnavailableError as exc:
            logger.warning("i_am_alive: table unavailable (%s)", exc)
            return
        if own_row is None:
            logger.warning(
                "Own row missing from membership table; skipping i_am_alive write",
            )
            return
        now = self._wall_clock()
        updated = with_replacements(own_row, i_am_alive=now, last_heartbeat=now)
        try:
            await self._membership.try_update_silo(updated)
        except TableStaleError as exc:
            logger.debug("i_am_alive etag collision (%s); will retry", exc)
            return
        except MembershipUnavailableError as exc:
            logger.warning("i_am_alive write: table unavailable (%s)", exc)
            return
        await self._broadcast_snapshot()

    async def _broadcast_snapshot(self) -> None:
        """Push the freshest snapshot to every connected peer, best-effort."""
        snapshot = await self._safe_read_all()
        if snapshot is None:
            return
        self._last_snapshot = snapshot
        body = encode_snapshot(snapshot)
        peers = [p for p in self._transport.get_connected_silos() if p != self._local]
        if not peers:
            return
        results = await asyncio.gather(
            *(self._safe_one_way(p, MEMBERSHIP_SNAPSHOT_HEADER, body) for p in peers),
            return_exceptions=True,
        )
        for peer, res in zip(peers, results, strict=True):
            if isinstance(res, BaseException):
                logger.debug("Snapshot broadcast to %s failed: %r", peer.silo_id, res)

    async def _safe_one_way(self, peer: SiloAddress, header: bytes, body: bytes) -> None:
        try:
            await self._transport.send_one_way(peer, header, body)
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("send_one_way to %s failed: %s", peer.silo_id, exc)
            raise

    async def _apply_snapshot(self, snapshot: MembershipSnapshot, *, source: SiloAddress) -> None:
        if self._last_snapshot is not None and snapshot.version < self._last_snapshot.version:
            logger.debug(
                "Ignoring stale snapshot from %s: v%d < local v%d",
                source.silo_id,
                snapshot.version,
                self._last_snapshot.version,
            )
            return
        self._last_snapshot = snapshot
        if self._snapshot_hook is not None:
            try:
                await self._snapshot_hook(snapshot)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("snapshot-received hook raised: %s", exc, exc_info=True)
        if self._own_row_is_dead(snapshot):
            await self._self_terminate()

    async def _safe_read_all(self) -> MembershipSnapshot | None:
        try:
            return await self._membership.read_all()
        except MembershipUnavailableError as exc:
            logger.warning("Membership table unavailable: %s", exc)
            return None

    async def _on_peer_disconnect(self, peer: SiloAddress, cause: Exception | None) -> None:
        del cause
        self._detector.bump_miss_count(peer.silo_id)
        logger.debug("Peer %s disconnected; miss_count bumped", peer.silo_id)

    def _own_row_is_dead(self, snapshot: MembershipSnapshot) -> bool:
        for silo in snapshot.silos:
            if silo.address == self._local and silo.status == SiloStatus.DEAD:
                return True
        return False

    async def _self_terminate(self) -> None:
        logger.critical(
            "Own silo %s observed as DEAD in membership table; self-terminating per Orleans §3.7",
            self._local.silo_id,
        )
        self._running = False
        if self._self_terminate_hook is not None:
            try:
                await self._self_terminate_hook()
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Self-terminate hook raised: %s", exc, exc_info=True)

    async def _sleep_with_drift(self, interval: float) -> None:
        scheduled_end = self._loop_clock() + interval
        await asyncio.sleep(interval)
        drift = max(0.0, self._loop_clock() - scheduled_end)
        self._detector.self_monitor.record_loop_drift(drift)

    def __repr__(self) -> str:
        state = "running" if self._running else "stopped"
        return f"MembershipAgent(local={self._local.silo_id}, state={state})"


# ---- Snapshot wire codec -----------------------------------------------------


def encode_snapshot(snapshot: MembershipSnapshot) -> bytes:
    """Serialise ``snapshot`` to the wire body for MEMBERSHIP_SNAPSHOT."""
    doc = {
        "version": snapshot.version,
        "silos": [_silo_to_json(s) for s in snapshot.silos],
    }
    return json.dumps(doc, separators=(",", ":")).encode("utf-8")


def decode_snapshot(data: bytes) -> MembershipSnapshot:
    """Parse a MEMBERSHIP_SNAPSHOT body. Raises :class:`ValueError` on malformed input."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid snapshot bytes: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("snapshot must be a JSON object")
    try:
        version = int(obj["version"])
        raw_silos = obj["silos"]
        if not isinstance(raw_silos, list):
            raise ValueError("silos must be a list")
        silos = [_silo_from_json(item) for item in raw_silos]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed snapshot: {exc}") from exc
    return MembershipSnapshot(version=version, silos=silos)


def _silo_to_json(silo: SiloInfo) -> dict[str, Any]:
    return {
        "host": silo.address.host,
        "port": silo.address.port,
        "epoch": silo.address.epoch,
        "status": silo.status.value,
        "last_heartbeat": silo.last_heartbeat,
        "start_time": silo.start_time,
        "cluster_id": silo.cluster_id,
        "gateway_port": silo.gateway_port,
        "version": silo.version,
        "i_am_alive": silo.i_am_alive,
        "suspicions": [
            {"suspecting_silo": v.suspecting_silo, "timestamp": v.timestamp}
            for v in silo.suspicions
        ],
        "etag": silo.etag,
    }


def _silo_from_json(obj: Any) -> SiloInfo:
    if not isinstance(obj, dict):
        raise ValueError("silo entry must be a JSON object")
    try:
        address = SiloAddress(
            host=str(obj["host"]),
            port=int(obj["port"]),
            epoch=int(obj["epoch"]),
        )
        status = SiloStatus(obj["status"])
        suspicions = [
            SuspicionVote(
                suspecting_silo=str(v["suspecting_silo"]),
                timestamp=float(v["timestamp"]),
            )
            for v in obj.get("suspicions", [])
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"silo fields: {exc}") from exc
    cluster_id_raw = obj.get("cluster_id")
    gateway_port_raw = obj.get("gateway_port")
    return SiloInfo(
        address=address,
        status=status,
        last_heartbeat=float(obj["last_heartbeat"]),
        start_time=float(obj["start_time"]),
        cluster_id=str(cluster_id_raw) if cluster_id_raw is not None else None,
        gateway_port=int(gateway_port_raw) if gateway_port_raw is not None else None,
        version=int(obj.get("version", 0)),
        i_am_alive=float(obj.get("i_am_alive", 0.0)),
        suspicions=suspicions,
        etag=str(obj["etag"]) if obj.get("etag") is not None else None,
    )
