"""Failure detector — probe protocol, suspicion voting, indirect probe, self-monitoring.

Implements the decision layer described in :doc:`../../../docs/tasks/task-02-11-failure-detector`:

* :meth:`FailureDetector.probe_targets` picks hash-ring successors of the
  local silo as the silos to probe.
* :meth:`FailureDetector.probe_once` issues one direct PING over the
  transport with a self-monitor-adjusted timeout.
* :meth:`FailureDetector.on_probe_result` updates per-peer miss counts
  and — once the miss threshold is crossed — drives the voting flow.
* :meth:`FailureDetector._attempt_vote` performs one OCC write cycle:
  either ``try_add_suspicion`` (accumulate) or, on the deciding vote,
  an indirect probe followed by an atomic ``try_update_silo`` with
  ``status=DEAD``.
* :class:`SelfMonitor` scores local stress and widens
  ``probe_timeout_per_attempt`` when loaded, so a stressed silo does
  not false-positive its peers.

The detector owns state and decisions. Event-loop orchestration (probe
loop, table poll loop, IAmAlive loop, snapshot broadcast) lives in
:class:`pyleans.cluster.membership_agent.MembershipAgent`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from pyleans.cluster.hash_ring import ConsistentHashRing
from pyleans.cluster.identity import ClusterId
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus, SuspicionVote
from pyleans.providers.errors import (
    MembershipUnavailableError,
    SiloNotFoundError,
    TableStaleError,
)
from pyleans.providers.membership import (
    MembershipProvider,
    MembershipSnapshot,
    with_replacements,
)
from pyleans.transport.cluster import IClusterTransport
from pyleans.transport.errors import (
    TransportConnectionError,
    TransportError,
    TransportTimeoutError,
)

logger = logging.getLogger(__name__)

INDIRECT_PROBE_HEADER: Final[bytes] = b"pyleans/indirect-probe/v1"
"""Wire-header tag identifying an INDIRECT_PROBE request to the receiver."""

INDIRECT_PROBE_RESP_OK: Final[bytes] = b"OK"
INDIRECT_PROBE_RESP_FAIL: Final[bytes] = b"FAIL"

_MAX_STALE_RETRY: Final[int] = 3


@dataclass(frozen=True)
class FailureDetectorOptions:
    """Tunables mirroring the Orleans 9/10 defaults; production tunes per cluster."""

    num_probed_silos: int = 3
    probe_timeout: float = 10.0
    probe_timeout_per_attempt: float = 5.0
    num_missed_probes_limit: int = 3
    num_votes_for_death_declaration: int = 2
    death_vote_expiration: float = 180.0
    indirect_probe_intermediaries: int = 2
    i_am_alive_interval: float = 30.0
    table_poll_interval: float = 10.0
    num_missed_table_i_am_alive_limit: int = 3

    def __post_init__(self) -> None:
        non_negative_ints = (
            "num_probed_silos",
            "num_missed_probes_limit",
            "num_votes_for_death_declaration",
            "indirect_probe_intermediaries",
            "num_missed_table_i_am_alive_limit",
        )
        for name in non_negative_ints:
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0, got {getattr(self, name)!r}")
        positive_floats = (
            "probe_timeout",
            "probe_timeout_per_attempt",
            "death_vote_expiration",
            "i_am_alive_interval",
            "table_poll_interval",
        )
        for name in positive_floats:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0, got {getattr(self, name)!r}")
        if self.num_votes_for_death_declaration < 1:
            raise ValueError("num_votes_for_death_declaration must be >= 1")


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single direct probe."""

    ok: bool
    rtt: float = 0.0
    reason: str | None = None


@dataclass
class PeerHealth:
    """Per-peer failure-detector state."""

    miss_count: int = 0
    consecutive_ok: int = 0
    last_probe_ok_time: float = 0.0
    last_rtt: float = 0.0


class SelfMonitor:
    """Scores local silo stress on 0..8 and widens own probe timeouts.

    Two inputs contribute to the score:

    * **Event-loop drift** — seconds of ``asyncio.sleep`` overshoot
      observed by the probe loop. Drift >= 1 s adds 3 points, 0.5-1 s
      adds 2, 0.1-0.5 s adds 1, < 0.1 s decays by 1.
    * **Probe-send failures** — each :class:`TransportError` raised
      locally (not a timeout/disconnect from the peer) adds 1 point;
      each subsequent successful probe decays by 1.

    A silo with score ``s`` multiplies ``probe_timeout_per_attempt`` by
    ``1 + s / 4``. Score 0 → base timeout; score 8 → 3x. This gives a
    stressed silo slack on its own probes while still counting missed
    probes against peers.
    """

    _MAX_SCORE: Final[int] = 8
    _DRIFT_BAD: Final[float] = 1.0
    _DRIFT_MEDIUM: Final[float] = 0.5
    _DRIFT_MILD: Final[float] = 0.1

    def __init__(self, options: FailureDetectorOptions) -> None:
        self._options = options
        self._drift_points = 0
        self._probe_send_failures = 0

    def record_loop_drift(self, drift_seconds: float) -> None:
        """Fold one observed loop-drift sample into the stress score."""
        if drift_seconds >= self._DRIFT_BAD:
            self._drift_points = min(self._drift_points + 3, self._MAX_SCORE)
        elif drift_seconds >= self._DRIFT_MEDIUM:
            self._drift_points = min(self._drift_points + 2, self._MAX_SCORE)
        elif drift_seconds >= self._DRIFT_MILD:
            self._drift_points = min(self._drift_points + 1, self._MAX_SCORE)
        else:
            self._drift_points = max(0, self._drift_points - 1)

    def record_probe_send_failure(self) -> None:
        self._probe_send_failures = min(self._probe_send_failures + 1, self._MAX_SCORE)

    def reset_probe_send_failure(self) -> None:
        if self._probe_send_failures > 0:
            self._probe_send_failures -= 1

    @property
    def score(self) -> int:
        return min(self._drift_points + self._probe_send_failures, self._MAX_SCORE)

    def adjusted_probe_timeout(self) -> float:
        return self._options.probe_timeout_per_attempt * (1.0 + self.score / 4.0)


# Injectable sampler for deterministic tests; accepts a population Sequence and
# the number of items to sample and returns a list of that size.
IntermediarySampler = Callable[[Sequence[SiloAddress], int], list[SiloAddress]]


class FailureDetector:
    """Voting and probing over :class:`IClusterTransport` + :class:`MembershipProvider`.

    Pure-decision layer: owns per-peer :class:`PeerHealth`, the
    :class:`SelfMonitor`, and the OCC write path to the membership
    table, but does not drive any event loop. The caller is expected to
    invoke :meth:`probe_once` + :meth:`on_probe_result` at a cadence of
    ``options.probe_timeout``.
    """

    def __init__(
        self,
        local_silo: SiloAddress,
        cluster_id: ClusterId,
        options: FailureDetectorOptions,
        transport: IClusterTransport,
        membership: MembershipProvider,
        *,
        wall_clock: Callable[[], float] = time.time,
        intermediary_sampler: IntermediarySampler | None = None,
    ) -> None:
        del cluster_id  # reserved for future multi-cluster filtering
        self._local = local_silo
        self._options = options
        self._transport = transport
        self._membership = membership
        self._wall_clock = wall_clock
        self._sample = intermediary_sampler or _default_sampler
        self._peer_health: dict[str, PeerHealth] = {}
        self._self_monitor = SelfMonitor(options)

    @property
    def self_monitor(self) -> SelfMonitor:
        return self._self_monitor

    @property
    def local_silo(self) -> SiloAddress:
        return self._local

    @property
    def options(self) -> FailureDetectorOptions:
        return self._options

    def peer_health(self, silo_id: str) -> PeerHealth:
        """Return the live :class:`PeerHealth` record for ``silo_id`` (creates on demand)."""
        return self._peer_health.setdefault(silo_id, PeerHealth())

    def bump_miss_count(self, silo_id: str) -> None:
        """Record one extra miss against ``silo_id``, capped at the limit.

        Used by the transport-disconnect fast-path: a lost connection is
        a hint, not a proof of death, so we fold it in as one miss.
        """
        health = self.peer_health(silo_id)
        health.miss_count = min(health.miss_count + 1, self._options.num_missed_probes_limit)

    def probe_targets(self, snapshot: MembershipSnapshot) -> list[SiloAddress]:
        """Pick the silos this detector should probe this cycle.

        Rule:

        * Single-silo cluster → ``[]`` (the local silo is trivially alive).
        * ``<= num_probed_silos`` other active peers → probe every one.
        * Otherwise → the ``num_probed_silos`` successors of the local
          silo on the consistent hash ring.
        """
        active_peers = [
            s.address
            for s in snapshot.silos
            if s.status == SiloStatus.ACTIVE and s.address != self._local
        ]
        if not active_peers:
            return []
        n = self._options.num_probed_silos
        if len(active_peers) <= n:
            return sorted(active_peers, key=lambda a: a.silo_id)
        ring = ConsistentHashRing([*active_peers, self._local])
        return ring.successors(self._local, n)

    async def probe_once(self, peer: SiloAddress) -> ProbeResult:
        """Issue one direct PING to ``peer`` using the adjusted per-attempt timeout."""
        timeout = self._self_monitor.adjusted_probe_timeout()
        try:
            rtt = await self._transport.send_ping(peer, timeout=timeout)
        except TransportTimeoutError:
            return ProbeResult(ok=False, reason="timeout")
        except TransportConnectionError as exc:
            return ProbeResult(ok=False, reason=f"connection: {exc}")
        except TransportError as exc:
            self._self_monitor.record_probe_send_failure()
            return ProbeResult(ok=False, reason=f"transport: {exc}")
        self._self_monitor.reset_probe_send_failure()
        return ProbeResult(ok=True, rtt=rtt)

    async def indirect_probe(
        self, peer: SiloAddress, intermediaries: Sequence[SiloAddress]
    ) -> bool:
        """Ask ``intermediaries`` to probe ``peer``; return True if any reports ok.

        Abort-the-vote semantic: a single positive report means the
        peer is alive from at least one perspective and the death vote
        must be cancelled. All-failure (including intermediary
        unreachable) proceeds with the declaration.
        """
        if not intermediaries:
            return False
        per_attempt = self._self_monitor.adjusted_probe_timeout()
        body = peer.silo_id.encode("utf-8")

        async def ask(mediator: SiloAddress) -> bool:
            try:
                _, resp_body = await self._transport.send_request(
                    mediator,
                    INDIRECT_PROBE_HEADER,
                    body,
                    timeout=per_attempt + 1.0,
                )
            except (TransportError, TimeoutError, asyncio.CancelledError):
                return False
            return resp_body == INDIRECT_PROBE_RESP_OK

        results = await asyncio.gather(
            *(ask(m) for m in intermediaries),
            return_exceptions=True,
        )
        return any(r is True for r in results)

    def pick_intermediaries(
        self, snapshot: MembershipSnapshot, peer: SiloAddress
    ) -> list[SiloAddress]:
        """Sample up to ``indirect_probe_intermediaries`` active silos, excluding peer + local."""
        candidates = [
            s.address
            for s in snapshot.silos
            if s.status == SiloStatus.ACTIVE and s.address != self._local and s.address != peer
        ]
        k = min(self._options.indirect_probe_intermediaries, len(candidates))
        if k == 0:
            return []
        return self._sample(candidates, k)

    async def handle_indirect_probe_request(self, body: bytes) -> bytes:
        """Serve an inbound INDIRECT_PROBE: direct-probe the suspect, return OK/FAIL."""
        try:
            suspect_id = body.decode("utf-8")
            suspect = SiloAddress.from_silo_id(suspect_id)
        except (UnicodeDecodeError, ValueError) as exc:
            logger.warning("Malformed INDIRECT_PROBE body: %s", exc)
            return INDIRECT_PROBE_RESP_FAIL
        result = await self.probe_once(suspect)
        return INDIRECT_PROBE_RESP_OK if result.ok else INDIRECT_PROBE_RESP_FAIL

    async def on_probe_result(
        self,
        peer: SiloAddress,
        result: ProbeResult,
        snapshot: MembershipSnapshot,
    ) -> bool:
        """Update ``peer``'s health; return True if the peer was declared DEAD this call."""
        health = self.peer_health(peer.silo_id)
        if result.ok:
            if health.miss_count > 0:
                logger.info(
                    "Peer %s recovered (was %d missed probes)",
                    peer.silo_id,
                    health.miss_count,
                )
            health.miss_count = 0
            health.consecutive_ok += 1
            health.last_probe_ok_time = self._wall_clock()
            health.last_rtt = result.rtt
            return False
        health.consecutive_ok = 0
        health.miss_count += 1
        logger.debug(
            "Probe to %s failed (%s); miss_count=%d/%d",
            peer.silo_id,
            result.reason,
            health.miss_count,
            self._options.num_missed_probes_limit,
        )
        if health.miss_count < self._options.num_missed_probes_limit:
            return False
        return await self._vote_or_declare(peer, snapshot)

    async def _vote_or_declare(self, peer: SiloAddress, _snapshot: MembershipSnapshot) -> bool:
        """Drive up to ``_MAX_STALE_RETRY`` OCC write attempts; return True if DEAD written."""
        for attempt in range(_MAX_STALE_RETRY):
            try:
                return await self._attempt_vote(peer)
            except TableStaleError as exc:
                logger.info(
                    "Table stale during vote on %s (%s, attempt %d/%d); re-reading",
                    peer.silo_id,
                    exc,
                    attempt + 1,
                    _MAX_STALE_RETRY,
                )
            except MembershipUnavailableError as exc:
                logger.warning(
                    "Table unavailable during vote on %s: %s -- suspending",
                    peer.silo_id,
                    exc,
                )
                return False
            except SiloNotFoundError:
                logger.info(
                    "Peer %s disappeared from table during vote; resetting miss_count",
                    peer.silo_id,
                )
                self.peer_health(peer.silo_id).miss_count = 0
                return False
        logger.warning(
            "Giving up vote on %s after %d stale retries",
            peer.silo_id,
            _MAX_STALE_RETRY,
        )
        return False

    async def _attempt_vote(self, peer: SiloAddress) -> bool:
        peer_row = await self._membership.read_silo(peer.silo_id)
        if peer_row is None:
            logger.info("Peer %s already gone from table; resetting miss_count", peer.silo_id)
            self.peer_health(peer.silo_id).miss_count = 0
            return False
        if peer_row.status == SiloStatus.DEAD:
            logger.info("Peer %s already DEAD in table; skipping vote", peer.silo_id)
            return True
        now = self._wall_clock()
        fresh_voters = self._fresh_distinct_voters(peer_row, now)
        if self._local.silo_id in fresh_voters:
            logger.debug("Already have a fresh vote on %s; not duplicating", peer.silo_id)
            return False
        new_vote = SuspicionVote(suspecting_silo=self._local.silo_id, timestamp=now)
        combined = fresh_voters | {self._local.silo_id}
        is_deciding = len(combined) >= self._options.num_votes_for_death_declaration
        expected_etag = peer_row.etag or ""
        if not is_deciding:
            await self._membership.try_add_suspicion(peer.silo_id, new_vote, expected_etag)
            logger.info(
                "Cast suspicion vote on %s (voters=%d/%d)",
                peer.silo_id,
                len(combined),
                self._options.num_votes_for_death_declaration,
            )
            return False
        # Deciding vote: issue indirect probe before flipping to DEAD.
        snapshot = await self._membership.read_all()
        intermediaries = self.pick_intermediaries(snapshot, peer)
        if await self.indirect_probe(peer, intermediaries):
            logger.info(
                "Indirect probe found %s alive; aborting death vote, keeping suspicion",
                peer.silo_id,
            )
            await self._membership.try_add_suspicion(peer.silo_id, new_vote, expected_etag)
            return False
        logger.error(
            "Declaring %s DEAD (voters=%d/%d, indirect probe failed across %d intermediaries)",
            peer.silo_id,
            len(combined),
            self._options.num_votes_for_death_declaration,
            len(intermediaries),
        )
        updated = with_replacements(
            peer_row,
            status=SiloStatus.DEAD,
            suspicions=[*peer_row.suspicions, new_vote],
        )
        await self._membership.try_update_silo(updated)
        return True

    def _fresh_distinct_voters(self, row: SiloInfo, now: float) -> set[str]:
        cutoff = now - self._options.death_vote_expiration
        return {v.suspecting_silo for v in row.suspicions if v.timestamp >= cutoff}


def _default_sampler(population: Sequence[SiloAddress], k: int) -> list[SiloAddress]:
    return random.sample(list(population), k)
