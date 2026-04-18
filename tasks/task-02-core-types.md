# Task 02: Core Identity Types

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-project-setup.md](task-01-project-setup.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Grain Interfaces, State Declaration
- [orleans-grains.md](../docs/orleans-grains.md) -- Section 2 (Grain Identity)
- [orleans-cluster.md](../docs/orleans-cluster.md) -- Silo Architecture

## Description

Implement the core identity types used throughout pyleans.

### Files to create
- `src/pyleans/pyleans/identity.py`

### Types

```python
@dataclass(frozen=True)
class GrainId:
    """Uniquely identifies a grain in the cluster.

    Grain keys are always strings. Unlike Orleans (which supports Guid, Int64,
    and compound keys), pyleans uses string keys exclusively. This is a permanent
    design decision -- Orleans encodes all key types as strings internally anyway.
    """
    grain_type: str    # e.g. "CounterGrain"
    key: str           # e.g. "my-counter-1"

@dataclass(frozen=True)
class SiloAddress:
    """Identifies a silo in the cluster."""
    host: str
    port: int
    epoch: int         # startup timestamp, unique across restarts

    @property
    def encoded(self) -> str:
        """URL/topic-safe encoding."""
        return f"{self.host}_{self.port}_{self.epoch}"

@dataclass
class SiloInfo:
    """Full silo metadata for the membership table."""
    address: SiloAddress
    status: SiloStatus        # Active, Dead, Joining
    last_heartbeat: float     # monotonic timestamp
    start_time: float

class SiloStatus(Enum):
    JOINING = "joining"
    ACTIVE = "active"
    SHUTTING_DOWN = "shutting_down"
    DEAD = "dead"
```

### Acceptance criteria

- [x] `GrainId` is hashable and usable as dict key
- [x] `SiloAddress` is hashable and usable as dict key
- [x] Types are JSON-serializable via dataclass fields
- [x] Unit tests for equality, hashing, encoding

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Files created
- `src/pyleans/pyleans/identity.py` — GrainId, SiloAddress, SiloInfo, SiloStatus
- `src/pyleans/test/test_identity.py` — 22 tests

### Key decisions
- `GrainId.__str__` returns `"Type/key"` format for readable logging.
- `SiloAddress.__str__` returns `"host:port"` for human display, `encoded` property for machine-safe keys.

### Deviations
- File at `src/pyleans/pyleans/identity.py` (not `src/pyleans/identity.py`) matching existing project layout.

### Test coverage
- 22 tests covering creation, equality, hashing, dict key usage, set membership, frozen enforcement, str/encoded output, empty keys, mutability of SiloInfo, and all SiloStatus enum values.