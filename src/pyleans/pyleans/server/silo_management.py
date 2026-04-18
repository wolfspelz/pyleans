"""SiloManagement — service providing silo metadata to grains."""

from __future__ import annotations

import platform
import socket
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyleans.server.silo import Silo


class SiloManagement:
    """Service providing silo metadata to grains.

    Created by the Silo and bound to every grain instance as
    ``self.silo_management`` by the runtime during activation.
    """

    def __init__(self, silo: Silo) -> None:
        self._silo = silo

    def get_info(self) -> dict[str, Any]:
        """Return a dictionary of silo properties."""
        silo = self._silo
        addr = silo._silo_address
        return {
            "silo_id": silo._silo_id,
            "host": addr.host,
            "hostname": socket.gethostname(),
            "platform": platform.system(),
            "port": addr.port,
            "gateway_port": silo.gateway_port,
            "epoch": addr.epoch,
            "status": "active" if silo.started else "stopped",
            "uptime_seconds": time.time() - addr.epoch,
            "grain_count": len(silo.runtime.activations),
            "idle_timeout": silo._idle_timeout,
        }
