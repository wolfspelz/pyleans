"""Core identity types for pyleans."""

from dataclasses import dataclass, field
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

    Unlike Orleans (which supports Guid, Int64, and compound keys),
    pyleans uses string keys exclusively. Orleans encodes all key types
    as strings internally anyway.
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
    def silo_id(self) -> str:
        """Canonical string form used in membership rows, logs, and handshake.

        Format: ``host:port:epoch`` — e.g. ``10.0.0.5:11111:1713441000``.
        Stable, lexicographic-comparable, human-readable.
        """
        return f"{self.host}:{self.port}:{self.epoch}"

    @property
    def encoded(self) -> str:
        """URL/topic-safe encoding of the address (underscores, not colons)."""
        return f"{self.host}_{self.port}_{self.epoch}"

    def __lt__(self, other: "SiloAddress") -> bool:
        """Deterministic order used for connection-dedup tie-breaking."""
        return self.silo_id < other.silo_id

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class SuspicionVote:
    """One silo's accusation that another silo is unreachable.

    Stored on the accused silo's row and drained by the failure detector
    (task-02-11) when enough distinct votes accumulate. Frozen because
    a vote is an immutable event — edits replace, not mutate.
    """

    suspecting_silo: str
    timestamp: float


@dataclass
class SiloInfo:
    """Full silo metadata for the membership table.

    Phase 1 populates ``address``, ``status``, ``last_heartbeat``, ``start_time``.
    Phase 2 adds ``cluster_id``, ``gateway_port``, ``i_am_alive`` (Orleans
    self-written liveness timestamp), ``suspicions`` (failure-detector
    votes), ``version`` (row monotonic counter), and ``etag`` (opaque
    optimistic-concurrency tag).
    """

    address: SiloAddress
    status: SiloStatus
    last_heartbeat: float
    start_time: float
    cluster_id: str | None = None
    gateway_port: int | None = None
    version: int = 0
    i_am_alive: float = 0.0
    suspicions: list[SuspicionVote] = field(default_factory=list)
    etag: str | None = None
