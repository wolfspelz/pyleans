"""YAML file-based membership provider with Phase 2 OCC schema."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any, cast

import yaml

from pyleans.errors import MembershipError
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus, SuspicionVote
from pyleans.providers.errors import SiloNotFoundError, TableStaleError
from pyleans.providers.membership import MembershipProvider, MembershipSnapshot

logger = logging.getLogger(__name__)


class YamlMembershipProvider(MembershipProvider):
    """Stores membership in a single YAML file with monotonic version and row ETags.

    File layout::

        cluster_id: dev-cluster
        version: 42
        silos:
          - id: "host_port_epoch"
            host: ...
            port: 11111
            epoch: 1713441000
            status: active
            cluster_id: dev-cluster
            gateway_port: 30000
            last_heartbeat: 1713441030.0
            i_am_alive: 1713441030.0
            start_time: 1713441000.0
            suspicions:
              - suspecting_silo: "other:11111:1"
                timestamp: 1713441010.0
            etag: "h-3afb2c"

    Every mutation serialises through an ``asyncio.Lock`` — the file-lock
    primitive from task-02-10 wraps this provider when multiple processes
    write the same file.
    """

    def __init__(self, file_path: str = "./data/membership.yaml") -> None:
        self._file_path = Path(file_path)
        self._lock = asyncio.Lock()

    async def read_all(self) -> MembershipSnapshot:
        async with self._lock:
            data = self._load_table()
        return _data_to_snapshot(data)

    async def try_update_silo(self, silo: SiloInfo) -> SiloInfo:
        async with self._lock:
            data = self._load_table()
            silos = cast(list[dict[str, Any]], data.setdefault("silos", []))
            silo_id = silo.address.silo_id
            index = _find_silo_index(silos, silo_id)
            if index is None:
                if silo.etag is not None:
                    raise SiloNotFoundError(
                        f"Silo {silo_id!r} not found — cannot update with etag {silo.etag!r}"
                    )
                new_row = _silo_to_dict(silo)
                silos.append(new_row)
                refreshed = self._finalise_write(data, new_row)
                return _dict_to_silo(refreshed)
            current = silos[index]
            if current.get("etag") != silo.etag:
                raise TableStaleError(
                    f"etag mismatch for {silo_id!r}: "
                    f"expected {silo.etag!r}, current {current.get('etag')!r}"
                )
            updated = _silo_to_dict(silo)
            silos[index] = updated
            refreshed = self._finalise_write(data, updated)
            return _dict_to_silo(refreshed)

    async def try_delete_silo(self, silo: SiloInfo) -> None:
        async with self._lock:
            data = self._load_table()
            silos = cast(list[dict[str, Any]], data.setdefault("silos", []))
            silo_id = silo.address.silo_id
            index = _find_silo_index(silos, silo_id)
            if index is None:
                return
            current = silos[index]
            if current.get("etag") != silo.etag:
                raise TableStaleError(
                    f"etag mismatch for {silo_id!r}: "
                    f"expected {silo.etag!r}, current {current.get('etag')!r}"
                )
            del silos[index]
            data["version"] = int(data.get("version", 0)) + 1
            try:
                self._file_path.parent.mkdir(parents=True, exist_ok=True)
                self._file_path.write_text(
                    yaml.dump(data, default_flow_style=False, sort_keys=False),
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.error("Failed to write membership file: %s", exc)
                raise MembershipError(f"Failed to write membership file: {exc}") from exc

    async def try_add_suspicion(
        self, silo_id: str, vote: SuspicionVote, expected_etag: str
    ) -> SiloInfo:
        async with self._lock:
            data = self._load_table()
            silos = cast(list[dict[str, Any]], data.setdefault("silos", []))
            index = _find_silo_index(silos, silo_id)
            if index is None:
                raise SiloNotFoundError(f"Silo {silo_id!r} not found")
            current = silos[index]
            if current.get("etag") != expected_etag:
                raise TableStaleError(
                    f"etag mismatch for {silo_id!r}: "
                    f"expected {expected_etag!r}, current {current.get('etag')!r}"
                )
            votes = cast(list[dict[str, Any]], current.setdefault("suspicions", []))
            votes.append({"suspecting_silo": vote.suspecting_silo, "timestamp": vote.timestamp})
            refreshed = self._finalise_write(data, current)
            return _dict_to_silo(refreshed)

    def _load_table(self) -> dict[str, Any]:
        if not self._file_path.exists():
            return _empty_table()
        try:
            content = self._file_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(content)
        except (OSError, yaml.YAMLError) as exc:
            logger.error("Failed to read membership file: %s", exc)
            raise MembershipError(f"Failed to read membership file: {exc}") from exc
        if not isinstance(parsed, dict):
            return _empty_table()
        parsed.setdefault("version", 0)
        parsed.setdefault("silos", [])
        return parsed

    def _finalise_write(self, data: dict[str, Any], target_row: dict[str, Any]) -> dict[str, Any]:
        data["version"] = int(data.get("version", 0)) + 1
        target_row["etag"] = _compute_etag(target_row, data["version"])
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_path.write_text(
                yaml.dump(data, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("Failed to write membership file: %s", exc)
            raise MembershipError(f"Failed to write membership file: {exc}") from exc
        return target_row

    def _read_file(self) -> dict[str, Any]:
        """Internal accessor kept for legacy-test compatibility."""
        return self._load_table()


def _empty_table() -> dict[str, Any]:
    return {"version": 0, "silos": []}


def _find_silo_index(silos: list[dict[str, Any]], silo_id: str) -> int | None:
    for i, entry in enumerate(silos):
        if _entry_silo_id(entry) == silo_id:
            return i
    return None


def _entry_silo_id(entry: dict[str, Any]) -> str:
    return f"{entry.get('host')}:{entry.get('port')}:{entry.get('epoch')}"


def _compute_etag(row: dict[str, Any], version: int) -> str:
    stripped = {k: v for k, v in row.items() if k != "etag"}
    serialised = yaml.dump(stripped, default_flow_style=False, sort_keys=True)
    digest = hashlib.sha256(f"{version}::{serialised}".encode()).hexdigest()
    return f"h-{digest[:12]}"


def _data_to_snapshot(data: dict[str, Any]) -> MembershipSnapshot:
    version = int(data.get("version", 0))
    raw_silos = cast(list[dict[str, Any]], data.get("silos", []))
    silos = [_dict_to_silo(entry) for entry in raw_silos]
    return MembershipSnapshot(version=version, silos=silos)


def _silo_to_dict(silo: SiloInfo) -> dict[str, Any]:
    return {
        "id": silo.address.encoded,
        "host": silo.address.host,
        "port": silo.address.port,
        "epoch": silo.address.epoch,
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
        "etag": silo.etag,
    }


def _dict_to_silo(entry: dict[str, Any]) -> SiloInfo:
    try:
        address = SiloAddress(
            host=str(entry["host"]),
            port=int(entry["port"]),
            epoch=int(entry["epoch"]),
        )
        status = SiloStatus(entry["status"])
        suspicions = [
            SuspicionVote(
                suspecting_silo=str(v["suspecting_silo"]),
                timestamp=float(v["timestamp"]),
            )
            for v in entry.get("suspicions", [])
        ]
        etag = entry.get("etag")
        return SiloInfo(
            address=address,
            status=status,
            last_heartbeat=float(entry["last_heartbeat"]),
            start_time=float(entry["start_time"]),
            cluster_id=_optional_str(entry.get("cluster_id")),
            gateway_port=_optional_int(entry.get("gateway_port")),
            i_am_alive=float(entry.get("i_am_alive", 0.0)),
            suspicions=suspicions,
            etag=str(etag) if etag is not None else None,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise MembershipError(f"Malformed silo entry in membership file: {exc}") from exc


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        return int(value)
    raise MembershipError(f"Cannot coerce {value!r} to int")
