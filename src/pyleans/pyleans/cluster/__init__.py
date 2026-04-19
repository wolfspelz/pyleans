"""Cluster-layer primitives for pyleans.

This package groups the building blocks that Phase 2 uses to enforce the
single-activation cluster contract: stable cluster-wide hashing, the
consistent hash ring that partitions ownership of grains, and — in
subsequent tasks — placement, transport, directory, and membership pieces.
"""

from pyleans.cluster.directory import DirectoryEntry, IGrainDirectory
from pyleans.cluster.distributed_directory import (
    ClusterNotReadyError,
    DistributedGrainDirectory,
)
from pyleans.cluster.failure_detector import (
    INDIRECT_PROBE_HEADER,
    INDIRECT_PROBE_RESP_FAIL,
    INDIRECT_PROBE_RESP_OK,
    FailureDetector,
    FailureDetectorOptions,
    PeerHealth,
    ProbeResult,
    SelfMonitor,
)
from pyleans.cluster.hash_ring import (
    VIRTUAL_NODES_PER_SILO,
    ConsistentHashRing,
    RingPosition,
)
from pyleans.cluster.identity import (
    ClusterId,
    hash_grain_id,
    hash_silo_virtual_node,
    stable_hash,
)
from pyleans.cluster.membership_agent import (
    MEMBERSHIP_SNAPSHOT_HEADER,
    MembershipAgent,
    decode_snapshot,
    encode_snapshot,
)
from pyleans.cluster.placement import (
    NoSilosAvailableError,
    PlacementStrategy,
    PreferLocalPlacement,
    RandomPlacement,
    get_placement_strategy,
    placement,
)

__all__ = [
    "INDIRECT_PROBE_HEADER",
    "INDIRECT_PROBE_RESP_FAIL",
    "INDIRECT_PROBE_RESP_OK",
    "MEMBERSHIP_SNAPSHOT_HEADER",
    "VIRTUAL_NODES_PER_SILO",
    "ClusterId",
    "ClusterNotReadyError",
    "ConsistentHashRing",
    "DirectoryEntry",
    "DistributedGrainDirectory",
    "FailureDetector",
    "FailureDetectorOptions",
    "IGrainDirectory",
    "MembershipAgent",
    "NoSilosAvailableError",
    "PeerHealth",
    "PlacementStrategy",
    "PreferLocalPlacement",
    "ProbeResult",
    "RandomPlacement",
    "RingPosition",
    "SelfMonitor",
    "decode_snapshot",
    "encode_snapshot",
    "get_placement_strategy",
    "hash_grain_id",
    "hash_silo_virtual_node",
    "placement",
    "stable_hash",
]
