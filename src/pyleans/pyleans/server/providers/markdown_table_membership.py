"""Markdown-table-based membership provider with Phase 2 OCC schema.

Same semantics as :class:`YamlMembershipProvider`; the difference is
the on-disk format — a Markdown table renders legibly in any viewer and
is the default provider for the dev-mode counter sample.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from pyleans.errors import MembershipError
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus, SuspicionVote
from pyleans.providers.errors import SiloNotFoundError, TableStaleError
from pyleans.providers.membership import MembershipProvider, MembershipSnapshot
from pyleans.server.providers.file_lock import FileLock, atomic_write

logger = logging.getLogger(__name__)

_FILE_LOCK_TIMEOUT = 5.0

_HEADER = (
    "| ID | Host | Port | Epoch | Status | ClusterId | GatewayPort "
    "| LastHeartbeat | IAmAlive | StartTime | Suspicions | ETag |"
)
_SEPARATOR = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
_COLUMN_COUNT = 12


@dataclass
class _State:
    version: int = 0
    silos: list[SiloInfo] = field(default_factory=list)


class MarkdownTableMembershipProvider(MembershipProvider):
    """Stores membership in a Markdown file as a human-readable OCC table.

    File layout::

        # Membership

        Version: N

        | ID | ... | ETag |
        | --- | ... | --- |
        | host_port_epoch | ... | h-xxxxxx |

    Sub-second precision is not preserved through the ISO-8601 timestamp
    columns; ``last_heartbeat`` and ``i_am_alive`` are rounded to the
    nearest second for readability.
    """

    def __init__(
        self,
        file_path: str = "./data/membership.md",
        *,
        file_lock_timeout: float = _FILE_LOCK_TIMEOUT,
    ) -> None:
        self._file_path = Path(file_path)
        self._lock = asyncio.Lock()
        self._file_lock_timeout = file_lock_timeout

    def _file_lock(self) -> FileLock:
        return FileLock(self._file_path, timeout=self._file_lock_timeout)

    async def read_all(self) -> MembershipSnapshot:
        async with self._lock, self._file_lock():
            state = self._load_table()
        return MembershipSnapshot(version=state.version, silos=list(state.silos))

    async def try_update_silo(self, silo: SiloInfo) -> SiloInfo:
        async with self._lock, self._file_lock():
            state = self._load_table()
            silo_id = silo.address.silo_id
            index = _find_silo_index(state.silos, silo_id)
            if index is None:
                if silo.etag is not None:
                    raise SiloNotFoundError(
                        f"Silo {silo_id!r} not found — cannot update with etag {silo.etag!r}"
                    )
                state.silos.append(silo)
                return await self._finalise_write(state, len(state.silos) - 1)
            current = state.silos[index]
            if current.etag != silo.etag:
                raise TableStaleError(
                    f"etag mismatch for {silo_id!r}: "
                    f"expected {silo.etag!r}, current {current.etag!r}"
                )
            state.silos[index] = _with_etag(silo, None)
            return await self._finalise_write(state, index)

    async def try_delete_silo(self, silo: SiloInfo) -> None:
        async with self._lock, self._file_lock():
            state = self._load_table()
            silo_id = silo.address.silo_id
            index = _find_silo_index(state.silos, silo_id)
            if index is None:
                return
            current = state.silos[index]
            if current.etag != silo.etag:
                raise TableStaleError(
                    f"etag mismatch for {silo_id!r}: "
                    f"expected {silo.etag!r}, current {current.etag!r}"
                )
            del state.silos[index]
            state.version += 1
            await self._write_state(state)

    async def try_add_suspicion(
        self, silo_id: str, vote: SuspicionVote, expected_etag: str
    ) -> SiloInfo:
        async with self._lock, self._file_lock():
            state = self._load_table()
            index = _find_silo_index(state.silos, silo_id)
            if index is None:
                raise SiloNotFoundError(f"Silo {silo_id!r} not found")
            current = state.silos[index]
            if current.etag != expected_etag:
                raise TableStaleError(
                    f"etag mismatch for {silo_id!r}: "
                    f"expected {expected_etag!r}, current {current.etag!r}"
                )
            current.suspicions.append(vote)
            return await self._finalise_write(state, index)

    def _load_table(self) -> _State:
        if not self._file_path.exists():
            return _State()
        try:
            content = self._file_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to read membership file: %s", exc)
            raise MembershipError(f"Failed to read membership file: {exc}") from exc
        return _parse(content)

    def _read_file(self) -> _State:
        """Internal accessor kept for legacy-test compatibility."""
        return self._load_table()

    async def _finalise_write(self, state: _State, target_index: int) -> SiloInfo:
        state.version += 1
        target = state.silos[target_index]
        fresh = _with_etag(target, _compute_etag(target, state.version))
        state.silos[target_index] = fresh
        await self._write_state(state)
        return fresh

    async def _write_state(self, state: _State) -> None:
        try:
            await atomic_write(self._file_path, _render(state).encode("utf-8"))
        except OSError as exc:
            logger.error("Failed to write membership file: %s", exc)
            raise MembershipError(f"Failed to write membership file: {exc}") from exc


def _find_silo_index(silos: list[SiloInfo], silo_id: str) -> int | None:
    for i, silo in enumerate(silos):
        if silo.address.silo_id == silo_id:
            return i
    return None


def _with_etag(silo: SiloInfo, etag: str | None) -> SiloInfo:
    return replace(silo, suspicions=list(silo.suspicions), etag=etag)


def _compute_etag(silo: SiloInfo, version: int) -> str:
    payload = json.dumps(
        {
            "silo_id": silo.address.silo_id,
            "status": silo.status.value,
            "cluster_id": silo.cluster_id,
            "gateway_port": silo.gateway_port,
            "last_heartbeat": silo.last_heartbeat,
            "i_am_alive": silo.i_am_alive,
            "start_time": silo.start_time,
            "suspicions": [
                {"suspecting_silo": v.suspecting_silo, "timestamp": v.timestamp}
                for v in silo.suspicions
            ],
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(f"{version}::{payload}".encode()).hexdigest()
    return f"h-{digest[:12]}"


def _render(state: _State) -> str:
    lines = [
        "# Membership",
        "",
        f"Version: {state.version}",
        "",
        _HEADER,
        _SEPARATOR,
    ]
    lines.extend(_render_row(silo) for silo in state.silos)
    lines.append("")
    return "\n".join(lines)


def _render_row(silo: SiloInfo) -> str:
    cells = [
        silo.address.encoded,
        silo.address.host,
        str(silo.address.port),
        str(silo.address.epoch),
        silo.status.value,
        silo.cluster_id or "",
        str(silo.gateway_port) if silo.gateway_port is not None else "",
        _format_timestamp(silo.last_heartbeat),
        _format_timestamp(silo.i_am_alive),
        _format_timestamp(silo.start_time),
        _render_suspicions(silo.suspicions),
        silo.etag or "",
    ]
    for cell in cells:
        if "|" in cell or "\n" in cell or "\r" in cell:
            raise MembershipError(f"Membership cell contains table-breaking character: {cell!r}")
    return "| " + " | ".join(cells) + " |"


def _render_suspicions(votes: list[SuspicionVote]) -> str:
    if not votes:
        return ""
    return ";".join(f"{vote.suspecting_silo}@{_format_timestamp(vote.timestamp)}" for vote in votes)


def _parse(content: str) -> _State:
    state = _State()
    in_table = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("version:"):
            state.version = _parse_version(stripped)
            continue
        if stripped == _HEADER:
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped:
            in_table = False
            continue
        if stripped.startswith("| ---"):
            continue
        if stripped.startswith("|"):
            state.silos.append(_parse_row(stripped))
    return state


def _parse_version(line: str) -> int:
    try:
        return int(line.split(":", 1)[1].strip())
    except (IndexError, ValueError) as exc:
        raise MembershipError(f"Malformed version line: {line!r}") from exc


def _parse_row(line: str) -> SiloInfo:
    cells = [c.strip() for c in line.strip("|").split("|")]
    if len(cells) != _COLUMN_COUNT:
        raise MembershipError(f"Malformed membership row: {line!r}")
    (
        _,
        host,
        port,
        epoch,
        status,
        cluster_id,
        gateway_port,
        last_heartbeat,
        i_am_alive,
        start_time,
        suspicions_cell,
        etag_cell,
    ) = cells
    try:
        address = SiloAddress(host=host, port=int(port), epoch=int(epoch))
        silo = SiloInfo(
            address=address,
            status=SiloStatus(status),
            last_heartbeat=_parse_timestamp(last_heartbeat),
            start_time=_parse_timestamp(start_time),
            cluster_id=cluster_id or None,
            gateway_port=int(gateway_port) if gateway_port else None,
            i_am_alive=_parse_timestamp(i_am_alive) if i_am_alive else 0.0,
            suspicions=_parse_suspicions(suspicions_cell),
            etag=etag_cell or None,
        )
        return silo
    except (ValueError, TypeError) as exc:
        raise MembershipError(f"Malformed membership row: {line!r}") from exc


def _parse_suspicions(cell: str) -> list[SuspicionVote]:
    if not cell:
        return []
    votes: list[SuspicionVote] = []
    for chunk in cell.split(";"):
        if "@" not in chunk:
            raise MembershipError(f"Malformed suspicion entry: {chunk!r}")
        suspecting, timestamp = chunk.rsplit("@", 1)
        votes.append(
            SuspicionVote(
                suspecting_silo=suspecting,
                timestamp=_parse_timestamp(timestamp),
            )
        )
    return votes


def _format_timestamp(ts: float) -> str:
    if ts == 0.0:
        return ""
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")


def _parse_timestamp(s: str) -> float:
    if not s:
        return 0.0
    return datetime.fromisoformat(s).timestamp()
