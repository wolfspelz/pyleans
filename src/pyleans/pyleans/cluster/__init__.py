"""Cluster-layer primitives for pyleans.

This package groups the building blocks that Phase 2 uses to enforce the
single-activation cluster contract: stable cluster-wide hashing, the
consistent hash ring that partitions ownership of grains, and — in
subsequent tasks — placement, transport, directory, and membership pieces.
"""

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

__all__ = [
    "VIRTUAL_NODES_PER_SILO",
    "ClusterId",
    "ConsistentHashRing",
    "RingPosition",
    "hash_grain_id",
    "hash_silo_virtual_node",
    "stable_hash",
]
