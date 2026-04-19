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

- [x] `ClusterId("dev-cluster")` validates and is hashable / usable as dict key
- [x] `ClusterId("bad/id")` raises `ValueError`
- [x] `SiloAddress.silo_id` returns `"host:port:epoch"` and is lexicographically orderable via `<`
- [x] `SiloInfo` backwards-compatible: Phase 1 construction `SiloInfo(address, status, last_heartbeat, start_time)` still works
- [x] `stable_hash(b"abc")` returns the same `int` across Python processes (test by spawning subprocess)
- [x] `hash_grain_id` and `hash_silo_virtual_node` are pure, deterministic, and covered
- [x] Unit tests cover happy path, validation, cross-process determinism, hash distribution smoke check

## Findings of code review

No open issues. Review checked:

- **Clean Code / SOLID / DRY / YAGNI / KISS**: `ClusterId` is a minimal frozen value type with post-init validation; `stable_hash` is one function with a single, documented contract; `hash_grain_id` / `hash_silo_virtual_node` are thin wrappers that encode the canonical string form, which is the DRY centralisation point every Phase 2 ring computation reuses.
- **Type hints**: fully typed, mypy strict clean.
- **Hexagonal architecture**: module sits in the core namespace (value type + pure function), not a provider. Correct location.
- **Tests**: AAA labels, exactly one Act per test, descriptive names, fast (<150 ms for 57 tests), deterministic. Cross-process determinism covered via a subprocess invocation with `PYTHONHASHSEED=random`. Distribution smoke check uses 10,000 keys across 16 buckets with a ±25% band per bucket.
- **No dead code or unused imports**. (One round of cleanup removed an unused `logger` during review — logging is deferred to subsystems that actually emit events.)
- **Naming**: `ClassName`, `lower_snake_case`, `_MODULE_CONSTANT`.

## Findings of security review

No open issues. Review of the changes and the surrounding codebase:

- **Input validation** on `ClusterId` rejects the two characters that break downstream encodings: `/` (membership row / topic separator) and `\0` (string termination in any C-backed layer). Wider validation (whitespace, control chars, length cap) is deferred — no current subsystem is broken by them, and rejecting them now would be speculative per YAGNI.
- **Hashing**: SHA-256 is cryptographic; we truncate to 64 bits for a non-auth placement function. Not an authentication or authorisation decision, so collision-resistance margin is irrelevant; the choice trades "no extra dep" against `xxhash`.
- **Subprocess in tests** uses `sys.executable` (absolute path), a literal command string, and `env={"PYTHONHASHSEED": "random", "PATH": ""}` — no injection vector.
- **No filesystem, network, or external-service access** introduced. No deserialisation. No unbounded allocations (`stable_hash` input is any size, output is always 8 bytes).
- **OWASP Top 10**: not applicable to this change.

## Summary of implementation

### Files created
- [src/pyleans/pyleans/cluster.py](../../src/pyleans/pyleans/cluster.py) — `ClusterId` value type, `stable_hash`, `hash_grain_id`, `hash_silo_virtual_node`.
- [src/pyleans/test/test_cluster.py](../../src/pyleans/test/test_cluster.py) — 27 unit tests.

### Files modified
- [src/pyleans/pyleans/identity.py](../../src/pyleans/pyleans/identity.py) — `SiloAddress` gained `silo_id` property and `__lt__`; `SiloInfo` gained optional `cluster_id`, `gateway_port`, `version` fields (all with safe defaults so Phase 1 callers are unchanged).
- [src/pyleans/test/test_identity.py](../../src/pyleans/test/test_identity.py) — tests added for `silo_id`, lexicographic ordering, and the new `SiloInfo` fields.
- [pyproject.toml](../../pyproject.toml) — added `types-PyYAML>=6.0` to dev deps so `mypy` strict passes out of the box (was a pre-existing gap surfaced by this task's verification pass).

### Key decisions
- **SHA-256 truncated to 64 bits, not `xxhash`**. Chosen to avoid an extra dependency; the PoC is not hash-throughput-bound. If profiling later shows `stable_hash` as a hot path, switching backends is a one-line change contained to `cluster.py`.
- **Canonical hash inputs**: `grain_type/grain_key` (slash separator, matches `GrainId.__str__`) and `silo_id#vnode_index` (hash separator, disambiguates virtual nodes from their owning silo). These two entry points are the only places in Phase 2 that compute ring positions from domain values — every other ring computation in the upcoming tasks routes through them, guaranteeing identical ring positions on every silo.
- **`SiloInfo` additions are additive**. Defaults (`cluster_id=None`, `gateway_port=None`, `version=0`) mean Phase 1 construction keeps working unchanged. Phase 2 will populate these in tasks 02-09 (membership extensions) and 02-17 (lifecycle stages).
- **Minimal `ClusterId` validation**. Reject only `/` and `\0` — the two characters that definitely break membership-row and null-terminated encodings. Wider policy (length, whitespace) is left to subsystems that impose the constraint.

### Deviations
- None from the task spec. One infra fix added: `types-PyYAML` in dev deps (pre-existing gap; unrelated to the task's feature scope but required for the task's mypy check to succeed on a fresh clone).

### Test coverage summary
57 tests covering: `ClusterId` validation (empty, slash, null byte), frozen semantics, equality, dict-key use, `str`; `stable_hash` return type, range, in-process determinism, distinctness, empty input, cross-process determinism (via subprocess with empty `PATH`), distribution smoke check across 16 buckets over 10,000 keys; `hash_grain_id` and `hash_silo_virtual_node` determinism, distinctness along each input axis, match against canonical string form; `SiloAddress.silo_id` format, `<` ordering, `sorted()` determinism; `SiloInfo` Phase 1 backwards compatibility and each new keyword field.
