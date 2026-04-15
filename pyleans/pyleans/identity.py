"""Core identity types for pyleans."""

from dataclasses import dataclass
from enum import Enum


class SiloStatus(Enum):
    """Lifecycle status of a silo in the cluster."""

    JOINING = "joining"
    ACTIVE = "active"
    SHUTTING_DOWN = "shutting_down"
    DEAD = "dead"


@dataclass(frozen=True)
class GrainId:
    """Uniquely identifies a grain in the cluster.

    A grain is identified by its type name and a string key.
    The combination must be unique across the cluster.
    """

    grain_type: str
    key: str

    def __str__(self) -> str:
        return f"{self.grain_type}/{self.key}"


@dataclass(frozen=True)
class SiloAddress:
    """Identifies a silo in the cluster.

    The epoch distinguishes silo restarts on the same host:port.
    """

    host: str
    port: int
    epoch: int

    @property
    def encoded(self) -> str:
        """URL/topic-safe encoding of the address."""
        return f"{self.host}_{self.port}_{self.epoch}"

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class SiloInfo:
    """Full silo metadata for the membership table."""

    address: SiloAddress
    status: SiloStatus
    last_heartbeat: float
    start_time: float
