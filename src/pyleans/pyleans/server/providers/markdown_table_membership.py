"""Markdown-table-based membership provider."""

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pyleans.errors import MembershipError
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus
from pyleans.providers.membership import MembershipProvider

logger = logging.getLogger(__name__)

_HEADER = "| ID | Host | Port | Epoch | Status | Last Heartbeat | Start Time |"
_SEPARATOR = "| --- | --- | --- | --- | --- | --- | --- |"
_COLUMN_COUNT = 7


@dataclass
class _State:
    version: int = 0
    silos: list[SiloInfo] = field(default_factory=list)


class MarkdownTableMembershipProvider(MembershipProvider):
    """Stores membership in a single Markdown file as a human-readable table.

    Intended for development: the file renders nicely in any Markdown viewer,
    making the cluster membership observable at a glance.

    File format::

        # Membership

        Version: N

        | ID | Host | Port | Epoch | Status | Last Heartbeat | Start Time |
        | --- | --- | --- | --- | --- | --- | --- |
        | host_port_epoch | host | port | epoch | status | ISO-8601 UTC | ISO-8601 UTC |

    Timestamps are rendered with second-level resolution for readability; sub-second
    precision is not preserved across a round trip.
    """

    def __init__(self, file_path: str = "./data/membership.md") -> None:
        self._file_path = Path(file_path)

    async def register_silo(self, silo: SiloInfo) -> None:
        state = self._read_file()
        silo_id = silo.address.encoded
        for i, entry in enumerate(state.silos):
            if entry.address.encoded == silo_id:
                state.silos[i] = silo
                self._write_file(state)
                logger.info("Silo re-registered: %s", silo_id)
                return
        state.silos.append(silo)
        self._write_file(state)
        logger.info("Silo registered: %s", silo_id)

    async def unregister_silo(self, silo_id: str) -> None:
        state = self._read_file()
        state.silos = [s for s in state.silos if s.address.encoded != silo_id]
        self._write_file(state)
        logger.info("Silo unregistered: %s", silo_id)

    async def get_active_silos(self) -> list[SiloInfo]:
        state = self._read_file()
        return [s for s in state.silos if s.status == SiloStatus.ACTIVE]

    async def heartbeat(self, silo_id: str) -> None:
        state = self._read_file()
        for entry in state.silos:
            if entry.address.encoded == silo_id:
                entry.last_heartbeat = time.time()
                self._write_file(state)
                logger.debug("Heartbeat updated for %s", silo_id)
                return
        raise MembershipError(f"Silo {silo_id!r} not found in membership table")

    async def update_status(self, silo_id: str, status: SiloStatus) -> None:
        state = self._read_file()
        for entry in state.silos:
            if entry.address.encoded == silo_id:
                entry.status = status
                self._write_file(state)
                logger.info("Status updated for %s: %s", silo_id, status.value)
                return
        raise MembershipError(f"Silo {silo_id!r} not found in membership table")

    def _read_file(self) -> _State:
        if not self._file_path.exists():
            return _State()
        try:
            content = self._file_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.error("Failed to read membership file: %s", e)
            raise MembershipError(f"Failed to read membership file: {e}") from e
        return _parse(content)

    def _write_file(self, state: _State) -> None:
        state.version += 1
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_path.write_text(_render(state), encoding="utf-8")
        except OSError as e:
            logger.error("Failed to write membership file: %s", e)
            raise MembershipError(f"Failed to write membership file: {e}") from e


def _render(state: _State) -> str:
    lines = [
        "# Membership",
        "",
        f"Version: {state.version}",
        "",
        _HEADER,
        _SEPARATOR,
    ]
    lines.extend(_render_row(s) for s in state.silos)
    lines.append("")
    return "\n".join(lines)


def _render_row(silo: SiloInfo) -> str:
    cells = [
        silo.address.encoded,
        silo.address.host,
        str(silo.address.port),
        str(silo.address.epoch),
        silo.status.value,
        _format_timestamp(silo.last_heartbeat),
        _format_timestamp(silo.start_time),
    ]
    for cell in cells:
        if "|" in cell or "\n" in cell or "\r" in cell:
            raise MembershipError(f"Membership cell contains table-breaking character: {cell!r}")
    return "| " + " | ".join(cells) + " |"


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
    except (IndexError, ValueError) as e:
        raise MembershipError(f"Malformed version line: {line!r}") from e


def _parse_row(line: str) -> SiloInfo:
    cells = [c.strip() for c in line.strip("|").split("|")]
    if len(cells) != _COLUMN_COUNT:
        raise MembershipError(f"Malformed membership row: {line!r}")
    _, host, port, epoch, status, last_heartbeat, start_time = cells
    try:
        return SiloInfo(
            address=SiloAddress(host=host, port=int(port), epoch=int(epoch)),
            status=SiloStatus(status),
            last_heartbeat=_parse_timestamp(last_heartbeat),
            start_time=_parse_timestamp(start_time),
        )
    except (ValueError, TypeError) as e:
        raise MembershipError(f"Malformed membership row: {line!r}") from e


def _format_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")


def _parse_timestamp(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()
