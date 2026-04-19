"""Cluster-level identity and deterministic hashing for pyleans.

Phase 2 introduces a cluster-level identifier (:class:`ClusterId`) plus a
process-stable 64-bit hash (:func:`stable_hash`) that every silo computes
identically. The hash is the foundation for the consistent hash ring, the
distributed grain directory, and probe-target selection.

Python's builtin :func:`hash` is randomised per process (``PYTHONHASHSEED``)
and cannot be used here — every silo must agree on positions on the ring.

See :doc:`../../docs/adr/adr-single-activation-cluster` for how this module's
outputs feed the three subsystems (membership, directory, cluster transport)
that jointly enforce the single-activation contract.
"""

import hashlib
from dataclasses import dataclass

from pyleans.identity import GrainId, SiloAddress

_HASH_BYTES = 8


@dataclass(frozen=True)
class ClusterId:
    """Identifies a cluster. All silos in one cluster share this value.

    Two silos with different :class:`ClusterId` values must refuse to
    handshake even if they reach each other's network endpoints — the
    transport wire protocol enforces this at connection setup. A single
    value is sufficient for the PoC; Orleans' split of ``ClusterId`` vs
    ``ServiceId`` is deferred.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value or "/" in self.value or "\0" in self.value:
            raise ValueError(f"invalid cluster id: {self.value!r}")

    def __str__(self) -> str:
        return self.value


def stable_hash(data: bytes) -> int:
    """Return a deterministic 64-bit hash of ``data``.

    The output is identical across processes, Python versions (3.12+), and
    platforms — SHA-256 truncated to the low 64 bits, interpreted as an
    unsigned big-endian integer. Chosen over ``xxhash`` to avoid an extra
    dependency; bias is negligible for our input cardinality.
    """
    digest = hashlib.sha256(data).digest()
    return int.from_bytes(digest[:_HASH_BYTES], byteorder="big", signed=False)


def hash_grain_id(grain_id: GrainId) -> int:
    """Hash a :class:`GrainId` to a 64-bit ring position."""
    return stable_hash(f"{grain_id.grain_type}/{grain_id.key}".encode())


def hash_silo_virtual_node(silo: SiloAddress, vnode_index: int) -> int:
    """Hash a silo virtual-node position on the ring.

    Per [adr-grain-directory](../../docs/adr/adr-grain-directory.md) each
    silo owns multiple virtual nodes (default 30) to smooth the distribution
    of grains across the ring.
    """
    return stable_hash(f"{silo.silo_id}#{vnode_index}".encode())
