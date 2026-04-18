# Task 02-09: Membership Table Extensions -- Versioning, ETags, Suspicion, IAmAlive

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-01-cluster-identity.md](task-02-01-cluster-identity.md)
- [task-01-12-yaml-membership.md](task-01-12-yaml-membership.md) (Phase 1 baseline provider to extend)

## References
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §3 protocol, §4.1 table schema, §4.2 consistency, §3.8 version row
- [adr-provider-interfaces](../adr/adr-provider-interfaces.md)
- Research: Orleans `IMembershipTable`, Hashicorp Consul session-based membership, etcd v3 lease model

## Description

Phase 1 models membership as "write row, read rows, heartbeat timestamp" -- enough for a single silo to report itself, not enough for distributed agreement. Phase 2 needs the table to serve as the **rendezvous point** for the failure detector ([task-02-11](task-02-11-failure-detector.md)): it must durably record suspicion votes and status transitions with optimistic concurrency so multiple silos writing concurrently reach a consistent outcome.

This task extends the `MembershipProvider` ABC and updates the YAML provider to the full schema. Concrete providers (Redis, SQL, etc.) in Phase 4 implement the same ABC.

### Files to modify/create

**Modify:**
- `src/pyleans/pyleans/providers/membership.py` -- extend ABC
- `src/pyleans/pyleans/identity.py` -- extend `SiloInfo` (already seeded with optional fields in [task-02-01](task-02-01-cluster-identity.md), now fully populated)
- `src/pyleans/pyleans/server/providers/yaml_membership.py` -- implement new schema + ETags + version row

**Create:**
- `src/pyleans/pyleans/providers/errors.py` -- `MembershipError`, `TableStaleError`, `SiloNotFoundError`

### Extended schema

```python
@dataclass(frozen=True)
class SuspicionVote:
    suspecting_silo: str     # silo_id of the accuser
    timestamp: float         # unix seconds; used with death_vote_expiration


@dataclass
class SiloInfo:
    address: SiloAddress
    status: SiloStatus
    last_heartbeat: float            # membership-layer heartbeat (phase 1 legacy)
    start_time: float
    cluster_id: str | None = None
    gateway_port: int | None = None
    i_am_alive: float = 0.0          # silo's self-written liveness timestamp (Orleans IAmAlive)
    suspicions: list[SuspicionVote] = field(default_factory=list)
    etag: str | None = None          # opaque version tag for optimistic writes


@dataclass(frozen=True)
class MembershipSnapshot:
    """A table read returns the full set plus the table version."""
    version: int                     # monotonic, increments on every write
    silos: list[SiloInfo]
```

`etag` is provider-opaque: YAML uses a content hash or simple counter, SQL uses `ROWVERSION`, Redis uses a per-row version number.

### Extended `MembershipProvider`

```python
class MembershipProvider(ABC):

    # --- Read (returns snapshot with etags attached so caller can do OCC writes) ---
    @abstractmethod
    async def read_all(self) -> MembershipSnapshot: ...

    @abstractmethod
    async def read_silo(self, silo_id: str) -> SiloInfo | None: ...

    # --- Write (optimistic concurrency) ---
    @abstractmethod
    async def try_update_silo(
        self,
        silo: SiloInfo,          # silo.etag carries the expected pre-image etag
    ) -> SiloInfo:
        """Atomic compare-and-swap on the row identified by silo.address.silo_id
        AND atomic increment of the version row.

        Returns the new SiloInfo (with fresh etag and the row's observed state).
        Raises TableStaleError if etag mismatch.
        """

    @abstractmethod
    async def try_add_suspicion(
        self,
        silo_id: str,
        vote: SuspicionVote,
        expected_etag: str,
    ) -> SiloInfo:
        """Append a suspicion to the silo's list. Separate method so providers
        with richer primitives (e.g. SQL, Redis) can implement it more
        efficiently than read-modify-write."""

    # --- Phase 1 convenience methods stay, implemented in terms of the new ABI ---
    async def register_silo(self, silo: SiloInfo) -> None: ...       # re-implemented
    async def unregister_silo(self, silo_id: str) -> None: ...
    async def heartbeat(self, silo_id: str) -> None:
        """Bumps i_am_alive (not last_heartbeat) to match Orleans semantics."""
    async def get_active_silos(self) -> list[SiloInfo]: ...
    async def update_status(self, silo_id: str, status: SiloStatus) -> None: ...
```

**Behavior contracts every provider must satisfy:**

1. **Every mutating operation is atomic** -- the target silo row and the version row update together or neither.
2. **Version row is monotonic non-decreasing** -- readers can compare snapshots.
3. **ETag check is mandatory** -- no "last write wins." Concurrent writers race; loser gets `TableStaleError` and retries.
4. **Reads are consistent within a snapshot** -- all silo rows returned by `read_all()` share the same table version.
5. **Unknown silo_id on `try_update_silo`** when the row does not exist is treated as "create" iff the caller's `silo.etag is None`. Otherwise: `SiloNotFoundError`. This lets `register_silo` reuse the same method.

### YAML provider implementation

Updated file layout:

```yaml
cluster_id: dev-cluster
version: 42
silos:
  "10.0.0.5:11111:1713441000":
    host: 10.0.0.5
    port: 11111
    epoch: 1713441000
    status: active
    gateway_port: 30000
    start_time: 2026-04-18T10:00:00Z
    last_heartbeat: 2026-04-18T10:00:30Z
    i_am_alive:     2026-04-18T10:00:30Z
    suspicions: []
    etag: "h-3afb2c"
```

ETag = stable hash of the row's canonicalized YAML representation + current version. On write:
1. Acquire file lock (implemented in [task-02-10](task-02-10-file-locking-membership.md)).
2. Load file.
3. Check the target row's current etag against the caller's expected etag.
4. On match: apply change, increment `version`, recompute etags, write temp file, atomic rename.
5. On mismatch: raise `TableStaleError` without modifying the file.

### Why file locking is a separate task

[Task-28](task-02-10-file-locking-membership.md) is a pure cross-cutting concern. This task describes the algorithm assuming the locking primitive is available; implementing it wrong is risky enough to deserve its own review boundary.

### MarkdownTableMembershipProvider

Same schema, same semantics, different file format. Phase 1 already ships the Markdown variant for human readability; this task extends it in parallel with the YAML provider. The conversion layer (dataclass ↔ Markdown rows) gets the same version + etag treatment.

### `read_silo` vs `read_all`

`read_silo` is a hot-path for the failure detector (poll one peer's row before writing a suspicion). `read_all` is periodic (reconcile local view). File-based providers implement `read_silo` as a filter over `read_all` -- the cost is negligible for PoC. SQL/Redis providers can target just the row.

### Error hierarchy

```python
class MembershipError(Exception): ...
class TableStaleError(MembershipError):
    """Optimistic concurrency check failed; caller should re-read and retry."""
class SiloNotFoundError(MembershipError): ...
class MembershipUnavailableError(MembershipError):
    """Table cannot be reached. Silo continues operating (accuracy > completeness,
    per Orleans §3.9)."""
```

`MembershipUnavailableError` is critical: the failure detector must distinguish "the table itself is down" from "a peer is down" and react differently (wait, don't cascade the suspicion).

### Acceptance criteria

- [ ] `MembershipSnapshot` returned by `read_all()` carries a monotonic `version`
- [ ] `try_update_silo` with correct etag succeeds and increments `version`
- [ ] `try_update_silo` with stale etag raises `TableStaleError` without modifying the file
- [ ] `try_add_suspicion` atomically appends; concurrent appenders serialize through OCC
- [ ] `heartbeat(silo_id)` updates `i_am_alive`, not `last_heartbeat`
- [ ] Phase 1 methods (`register_silo`, `get_active_silos`, etc.) continue to work against the new schema
- [ ] YAML file is human-readable and round-trips through edit-save-reload
- [ ] `MarkdownTableMembershipProvider` shares the same ABI tests
- [ ] Concurrency test: 5 async tasks race to append suspicions; exactly 5 appear in the final row
- [ ] Unit tests cover happy path, stale etag, unknown silo, and malformed-file recovery

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
