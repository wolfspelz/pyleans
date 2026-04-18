# Task 02-01: Cluster Identity -- `ClusterId`, Silo Id Format, Grain Id Hashing

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-02-core-types.md](task-01-02-core-types.md)

## References
- [adr-grain-directory](../adr/adr-grain-directory.md)
- [architecture/pyleans-transport.md](../architecture/pyleans-transport.md) -- §4.1 handshake (cluster_id, silo_id format)
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §2 ClusterId / ServiceId, §3.8 membership version row
- [orleans-networking.md](../orleans-architecture/orleans-networking.md) -- §6.3 silo identity `ip:port:epoch`
- [plan.md](../plan.md) -- Phase 2 item 1

## Description

Phase 2 introduces multiple silos that must mutually identify themselves, their cluster, and the grains they host. Phase 1 already ships `GrainId` and `SiloAddress`, but they lack two things needed for networking:

1. A **cluster-level identifier** (`ClusterId`) that every silo and every membership entry is scoped to. This prevents cross-cluster accidents (a silo from dev accidentally joining prod) and is required by the transport handshake.
2. A **canonical silo id string** (`host:port:epoch`) used in logs, membership rows, transport handshakes, and the hash ring. The string form must be stable and comparison-safe (lexicographic order is used for connection-dedup tie-breaking in [task-02-07](task-02-07-silo-connection-manager.md)).

Additionally, this task adds a **deterministic non-cryptographic hash** over `GrainId` and `SiloAddress` that every silo computes identically. Python's built-in `hash()` is process-local (randomized `PYTHONHASHSEED`) and unusable here. All placement decisions downstream depend on this hash, so it must land first.

### Files to create/modify

**Create:**
- `src/pyleans/pyleans/cluster.py` -- `ClusterId` value type, `stable_hash()` function

**Modify:**
- `src/pyleans/pyleans/identity.py` -- add `silo_id` property on `SiloAddress`, add `encoded` alias docstring note, add `__lt__`/sort order for deterministic tie-break
- `src/pyleans/pyleans/identity.py` -- `SiloInfo` gains optional `cluster_id`, `gateway_port`, `version` fields (non-breaking defaults)
- Tests

### Design

```python
# src/pyleans/pyleans/cluster.py
from dataclasses import dataclass

@dataclass(frozen=True)
class ClusterId:
    """Identifies a cluster. All silos in one cluster share this value.

    Two silos with different ClusterIds MUST refuse to handshake even if they
    reach each other's network endpoints (see task-02-05 wire protocol).
    """
    value: str

    def __post_init__(self) -> None:
        if not self.value or "/" in self.value or "\0" in self.value:
            raise ValueError(f"invalid cluster id: {self.value!r}")


def stable_hash(data: bytes) -> int:
    """Deterministic 64-bit hash, identical across processes and Python versions.

    Used for the consistent hash ring, directory partitioning, and probe-target
    selection. Implementation: SHA-256 truncated to the low 64 bits.
    Python's builtin hash() is NOT usable (PYTHONHASHSEED randomization).
    """
    ...
```

```python
# src/pyleans/pyleans/identity.py (additions)

@dataclass(frozen=True)
class SiloAddress:
    host: str
    port: int
    epoch: int

    @property
    def silo_id(self) -> str:
        """Canonical string form used in membership rows, logs, handshake.

        Format: "host:port:epoch" -- e.g. "10.0.0.5:11111:1713441000".
        Stable, lexicographic-comparable, human-readable.
        """
        return f"{self.host}:{self.port}:{self.epoch}"

    @property
    def encoded(self) -> str:
        """URL/topic-safe variant (underscores instead of colons)."""
        return f"{self.host}_{self.port}_{self.epoch}"

    def __lt__(self, other: "SiloAddress") -> bool:
        """Deterministic order used for connection-dedup tie-breaking."""
        return self.silo_id < other.silo_id
```

`SiloInfo` grows three optional fields (all default-safe so Phase 1 callers are unchanged):

- `cluster_id: str | None = None`
- `gateway_port: int | None = None` -- populated so clients can discover gateways from the membership table ([orleans-cluster.md §9.2](../orleans-architecture/orleans-cluster.md))
- `version: int = 0` -- monotonic version attached by the membership table at write time (full use in [task-02-09](task-02-09-membership-table-extensions.md))

### Hashing contract

`stable_hash(b"...")` must return the same `int` for the same bytes on every Python 3.12+ process. Implementation uses `hashlib.sha256(data).digest()[:8]` and `int.from_bytes(..., "big", signed=False)`. Rationale: SHA-256 is available everywhere in the standard library, bias is negligible for our input cardinality, and truncation to 64 bits keeps ring positions int-sized. `xxhash` would be faster but adds a dependency — revisit only if profiling shows this as a hot path.

Helper functions:

```python
def hash_grain_id(grain_id: GrainId) -> int:
    return stable_hash(f"{grain_id.grain_type}/{grain_id.key}".encode("utf-8"))

def hash_silo_virtual_node(silo: SiloAddress, vnode_index: int) -> int:
    return stable_hash(f"{silo.silo_id}#{vnode_index}".encode("utf-8"))
```

These are the only two hash entry points the rest of Phase 2 uses — every other "where does grain X live" calculation routes through them, so behavior stays identical across silos.

### Acceptance criteria

- [ ] `ClusterId("dev-cluster")` validates and is hashable / usable as dict key
- [ ] `ClusterId("bad/id")` raises `ValueError`
- [ ] `SiloAddress.silo_id` returns `"host:port:epoch"` and is lexicographically orderable via `<`
- [ ] `SiloInfo` backwards-compatible: Phase 1 construction `SiloInfo(address, status, last_heartbeat, start_time)` still works
- [ ] `stable_hash(b"abc")` returns the same `int` across Python processes (test by spawning subprocess)
- [ ] `hash_grain_id` and `hash_silo_virtual_node` are pure, deterministic, and covered
- [ ] Unit tests cover happy path, validation, cross-process determinism, hash distribution smoke check

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
