# Task 02-02: Consistent Hash Ring with Virtual Nodes

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-01-cluster-identity.md](task-02-01-cluster-identity.md)

## References
- [adr-grain-directory](../adr/adr-grain-directory.md)
- [architecture/consistent-hash-ring.md](../architecture/consistent-hash-ring.md) -- full design rationale
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §5 ring for probe target selection, §6.2 ring for directory partitioning
- [plan.md](../plan.md) -- Phase 2 item 4

## Description

The hash ring is the building block both the failure detector ([task-02-11](task-02-11-failure-detector.md)) and the distributed grain directory ([task-02-13](task-02-13-distributed-grain-directory.md)) consume. This task implements it as a standalone pure data structure with no networking or membership-table coupling -- the ring is given a list of silos as input and answers "who owns this key?" / "who are my N successors?" deterministically.

Landing the ring as a dedicated task early means downstream tasks can be built against a well-tested primitive instead of reinventing placement math inside each consumer.

### Files to create

- `src/pyleans/pyleans/cluster/hash_ring.py`
- `src/pyleans/pyleans/cluster/__init__.py`

### Design

```python
# src/pyleans/pyleans/cluster/hash_ring.py
from dataclasses import dataclass
from pyleans.identity import SiloAddress

VIRTUAL_NODES_PER_SILO = 30


@dataclass(frozen=True)
class RingPosition:
    position: int        # 64-bit hash
    silo: SiloAddress    # physical silo owning this virtual node
    vnode_index: int     # 0..VIRTUAL_NODES_PER_SILO-1


class ConsistentHashRing:
    """Immutable consistent hash ring with virtual nodes.

    Construction is O(N*V log N*V) (sort). Lookups are O(log N*V)
    (binary search). The ring is rebuilt from scratch on membership
    change -- N stays small, so rebuilding is cheaper than incremental
    maintenance and avoids subtle bugs under churn.
    """

    def __init__(
        self,
        silos: list[SiloAddress],
        virtual_nodes_per_silo: int = VIRTUAL_NODES_PER_SILO,
    ) -> None: ...

    def owner_of(self, key_hash: int) -> SiloAddress | None:
        """Return the silo that owns the arc containing key_hash.

        Walks clockwise: returns the silo at the first position >= key_hash,
        wrapping around to the lowest position if key_hash > max.
        Returns None only if the ring is empty.
        """

    def successors(self, silo: SiloAddress, count: int) -> list[SiloAddress]:
        """Return the next `count` distinct successor silos of `silo` on the ring.

        Used by the failure detector to pick probe targets (Orleans'
        NumProbedSilos). If `count` >= active silo count - 1, returns
        all other silos. Order is stable (same on every silo given same input).
        """

    @property
    def silos(self) -> list[SiloAddress]:
        """Snapshot of the silos currently on the ring (sorted by silo_id)."""

    def __len__(self) -> int:
        """Number of silos on the ring (physical, not virtual)."""

    def __contains__(self, silo: SiloAddress) -> bool: ...
```

### Determinism invariants

Two silos constructing a `ConsistentHashRing` from the same sorted silo list MUST produce identical `owner_of()` and `successors()` answers for any input. Tests must assert this explicitly:

1. Build the ring on two threads with shuffled input lists -- answers match.
2. Build across subprocesses -- answers match (closes the `PYTHONHASHSEED` risk).

### Load balance smoke test

With N=10 silos and V=30, the arc share per silo should fall within ±30% of `1/N`. This is a sanity check, not a production SLO -- the test asserts a generous bound to keep CI stable while catching regressions like "V accidentally dropped to 1."

### Empty / single-silo cases

- Empty ring: `owner_of(h)` returns `None`; `successors(s, k)` returns `[]`; `len()` is 0.
- Single silo: `owner_of(h)` returns that silo for any `h`; `successors(s, k)` returns `[]`.

These are exercised in tests since Phase 2 silos pass through "first silo joins" → "single-silo steady state" → "second silo joins" transitions.

### Acceptance criteria

- [ ] `ConsistentHashRing([])` behaves per empty-case contract
- [ ] `ConsistentHashRing([s1])` returns `s1` for any key
- [ ] `ConsistentHashRing([s1, s2, s3])` places `N*V` positions and answers `owner_of` via binary search
- [ ] Adding a silo remaps approximately `1/N` of the key space (test with K=10_000 random keys: changed owners fall within 5%-25% bound)
- [ ] `successors(s, 3)` returns 3 distinct silos in deterministic order; `successors(s, 100)` returns all other silos
- [ ] Cross-process determinism test (subprocess builds ring and emits owner_of answers — parent compares)
- [ ] Load-balance smoke test: N=10, V=30 — each silo owns between 0.7/N and 1.3/N of the ring
- [ ] Unit tests cover happy path, single silo, empty ring, rebuild under churn, successor exhaustion

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
