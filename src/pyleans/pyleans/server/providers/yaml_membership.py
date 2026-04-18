"""YAML file-based membership provider."""

import logging
import time
from pathlib import Path
from typing import Any

import yaml

from pyleans.errors import MembershipError
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus
from pyleans.providers.membership import MembershipProvider

logger = logging.getLogger(__name__)


class YamlMembershipProvider(MembershipProvider):
    """Stores membership in a single YAML file.

    File format:
        version: 1
        silos:
          - id: "host_port_epoch"
            host: "..."
            port: 11111
            epoch: 1713180000
            status: "active"
            last_heartbeat: 1713180060.0
            start_time: 1713180000.0
    """

    def __init__(self, file_path: str = "./data/membership.yaml") -> None:
        self._file_path = Path(file_path)

    async def register_silo(self, silo: SiloInfo) -> None:
        data = self._read_file()
        silo_id = silo.address.encoded
        silos = data["silos"]

        for i, entry in enumerate(silos):
            if entry["id"] == silo_id:
                silos[i] = self._silo_to_dict(silo)
                self._write_file(data)
                logger.info("Silo re-registered: %s", silo_id)
                return

        silos.append(self._silo_to_dict(silo))
        self._write_file(data)
        logger.info("Silo registered: %s", silo.address.encoded)

    async def unregister_silo(self, silo_id: str) -> None:
        data = self._read_file()
        data["silos"] = [s for s in data["silos"] if s["id"] != silo_id]
        self._write_file(data)
        logger.info("Silo unregistered: %s", silo_id)

    async def get_active_silos(self) -> list[SiloInfo]:
        data = self._read_file()
        return [
            self._dict_to_silo(entry)
            for entry in data["silos"]
            if entry["status"] == SiloStatus.ACTIVE.value
        ]

    async def heartbeat(self, silo_id: str) -> None:
        data = self._read_file()
        for entry in data["silos"]:
            if entry["id"] == silo_id:
                entry["last_heartbeat"] = time.time()
                self._write_file(data)
                logger.debug("Heartbeat updated for %s", silo_id)
                return
        raise MembershipError(f"Silo {silo_id!r} not found in membership table")

    async def update_status(self, silo_id: str, status: SiloStatus) -> None:
        data = self._read_file()
        for entry in data["silos"]:
            if entry["id"] == silo_id:
                entry["status"] = status.value
                self._write_file(data)
                logger.info("Status updated for %s: %s", silo_id, status.value)
                return
        raise MembershipError(f"Silo {silo_id!r} not found in membership table")

    def _read_file(self) -> dict[str, Any]:
        if not self._file_path.exists():
            return {"version": 0, "silos": []}
        try:
            content = self._file_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
        except (OSError, yaml.YAMLError) as e:
            logger.error("Failed to read membership file: %s", e)
            raise MembershipError(f"Failed to read membership file: {e}") from e
        if not isinstance(data, dict):
            return {"version": 0, "silos": []}
        return data

    def _write_file(self, data: dict[str, Any]) -> None:
        data["version"] = data.get("version", 0) + 1
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_path.write_text(
                yaml.dump(data, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error("Failed to write membership file: %s", e)
            raise MembershipError(f"Failed to write membership file: {e}") from e

    @staticmethod
    def _silo_to_dict(silo: SiloInfo) -> dict[str, Any]:
        return {
            "id": silo.address.encoded,
            "host": silo.address.host,
            "port": silo.address.port,
            "epoch": silo.address.epoch,
            "status": silo.status.value,
            "last_heartbeat": silo.last_heartbeat,
            "start_time": silo.start_time,
        }

    @staticmethod
    def _dict_to_silo(entry: dict[str, Any]) -> SiloInfo:
        try:
            return SiloInfo(
                address=SiloAddress(
                    host=str(entry["host"]),
                    port=int(entry["port"]),
                    epoch=int(entry["epoch"]),
                ),
                status=SiloStatus(entry["status"]),
                last_heartbeat=float(entry["last_heartbeat"]),
                start_time=float(entry["start_time"]),
            )
        except (KeyError, ValueError, TypeError) as e:
            raise MembershipError(f"Malformed silo entry in membership file: {e}") from e
