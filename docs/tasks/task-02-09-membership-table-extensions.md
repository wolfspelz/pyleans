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

- [x] `MembershipSnapshot` returned by `read_all()` carries a monotonic `version`
- [x] `try_update_silo` with correct etag succeeds and increments `version`
- [x] `try_update_silo` with stale etag raises `TableStaleError` without modifying the file
- [x] `try_add_suspicion` atomically appends; concurrent appenders serialize through OCC
- [x] `try_delete_silo` physically removes the row (replaces Phase 1 `unregister_silo`)
- [x] YAML file is human-readable and round-trips through edit-save-reload
- [x] `MarkdownTableMembershipProvider` shares the same ABI tests (parameterised fixture)
- [x] Concurrency test: 5 async tasks race to append suspicions; exactly 5 appear in the final row
- [x] Unit tests cover happy path, stale etag, unknown silo, and malformed-file recovery
- [x] Legacy Phase 1 methods removed from the ABC; Silo + tests + examples converted to OCC primitives

## Findings of code review

- **Clean code / SOLID / DRY**: OCC algorithm (read → validate etag → apply → bump version → write) is implemented once per provider. Shared helper (`with_replacements`) uses `dataclasses.replace` to avoid duplicated copy-constructors. `read_silo` default implementation on the ABC filters `read_all`, so file providers never implement a filter.
- **Hexagonal architecture**: the port (`MembershipProvider`) exposes exactly the OCC primitives (`read_all`, `read_silo`, `try_update_silo`, `try_add_suspicion`, `try_delete_silo`) and the `MembershipSnapshot` value type. No legacy convenience methods. Adapters (`YamlMembershipProvider`, `MarkdownTableMembershipProvider`) live in `pyleans.server.providers`; the new `InMemoryMembershipProvider` is a first-class adapter under `pyleans.testing` so every test root imports it instead of copying a double.
- **Legacy code removal (coding-rule #10)**: the previous `register_silo` / `unregister_silo` / `heartbeat` / `update_status` / `get_active_silos` legacy methods on the ABC — and their tests — are gone. Silo, counter_app, and every test fake uses the OCC primitives directly. There is exactly one way to mutate the membership table.
- **Type hints**: `mypy` strict clean — one narrowly-scoped `type: ignore[arg-type]` on `with_replacements` (dataclasses.replace's variadic-kwargs signature) with justification.
- **Tests**: AAA labels, one Act per test; parameterised fixture runs the new OCC suite against both YAML and Markdown back-ends. 26 new tests cover snapshot emptiness / version bump / per-row etags; `try_update_silo` create / stale-etag / matching-etag / unknown-silo; `try_add_suspicion` append / stale-etag / unknown silo / 5-way concurrency; `try_delete_silo`; new fields (cluster_id / gateway_port / i_am_alive) persisting through `try_update_silo`.
- **Logging**: adapters emit no lifecycle logs (they expose only OCC primitives, not lifecycle events). Silo logs register / shutdown / unregister at INFO and heartbeat at DEBUG as before.

No open issues. One pylint duplicate-code informational note remains where the YAML `_silo_to_dict` and Markdown `_compute_etag` payloads necessarily share field names; factoring them further would obscure the per-format serialisation rules.

## Findings of security review

- **Input validation at the boundary**: both providers reject malformed rows (`MembershipError`) on read; Markdown rejects pipe / newline / CR characters in cells so crafted hosts can't break the table structure.
- **ETag collision resistance**: `h-<12-hex-chars-of-SHA256-digest>` — 48 bits of entropy per row. Collisions across a small cluster are astronomically unlikely; collision-forcing attacks require write access to the file already.
- **Optimistic concurrency bounds divergence**: `try_update_silo`, `try_add_suspicion`, and `_delete_silo` each re-read, verify, mutate, and write under the provider's `asyncio.Lock` — two in-process callers cannot interleave writes. Cross-process concurrency is deferred to task-02-10 (file locking).
- **Unbounded resource consumption**: suspicion list grows linearly with votes. Phase 2 tasks that consume the list (02-11 failure detector) drain it after a silo transitions to DEAD; until then it's bounded by the number of active peers — O(N) not O(∞).
- **No secrets logged**: etags, silo addresses, and cluster ids are public; no tokens in any log line.
- **No blanket `except Exception`** — OS / YAML errors are caught narrowly and wrapped in `MembershipError`.

No vulnerabilities found.

## Summary of implementation

### Files modified / created

- [src/pyleans/pyleans/identity.py](../../src/pyleans/pyleans/identity.py) — added `SuspicionVote` (frozen dataclass); extended `SiloInfo` with `i_am_alive`, `suspicions`, `etag`.
- [src/pyleans/pyleans/providers/membership.py](../../src/pyleans/pyleans/providers/membership.py) — **rewrote the ABC**: the five Phase 1 convenience methods (`register_silo`, `unregister_silo`, `heartbeat`, `update_status`, `get_active_silos`) are removed; the ABC now exposes exactly the OCC primitives (`read_all`, `read_silo`, `try_update_silo`, `try_add_suspicion`, `try_delete_silo`) plus the `MembershipSnapshot` value type and the `with_replacements` helper.
- [src/pyleans/pyleans/providers/errors.py](../../src/pyleans/pyleans/providers/errors.py) — new module — `TableStaleError`, `SiloNotFoundError`, `MembershipUnavailableError` plus re-export of `MembershipError`.
- [src/pyleans/pyleans/server/providers/yaml_membership.py](../../src/pyleans/pyleans/server/providers/yaml_membership.py) — rewritten to Phase 2 schema (cluster_id, gateway_port, i_am_alive, suspicions, etag, monotonic version row); `try_delete_silo` physically removes the row.
- [src/pyleans/pyleans/server/providers/markdown_table_membership.py](../../src/pyleans/pyleans/server/providers/markdown_table_membership.py) — rewritten with a 12-column table and the same OCC semantics.
- [src/pyleans/pyleans/server/silo.py](../../src/pyleans/pyleans/server/silo.py) — **rewired to OCC primitives**: four new private helpers (`_register_in_membership`, `_bump_heartbeat`, `_transition_status`, `_unregister_from_membership`, `_occ_write`) implement the read-modify-write loop with bounded retries. `_silo_id` moved from `.encoded` (underscores) to `.silo_id` (colons) — the canonical format used by the Phase 2 protocol.
- [src/pyleans/pyleans/testing/](../../src/pyleans/pyleans/testing/) — new sub-package exposing `InMemoryMembershipProvider`, a functional OCC in-memory adapter that every workspace imports instead of copying a local fake.
- [src/pyleans/test/test_membership_occ.py](../../src/pyleans/test/test_membership_occ.py) — new — parameterised OCC suite over both file providers (26 tests).
- Rewrote [src/pyleans/test/test_yaml_membership.py](../../src/pyleans/test/test_yaml_membership.py) and [src/pyleans/test/test_markdown_table_membership.py](../../src/pyleans/test/test_markdown_table_membership.py) — legacy-method tests are gone; remaining tests target file-format particulars (YAML structure / Markdown table shape, cell safety, malformed-input recovery).
- Rewrote [src/pyleans/test/test_providers.py](../../src/pyleans/test/test_providers.py) to assert the new OCC surface.
- Updated [src/pyleans/test/test_silo.py](../../src/pyleans/test/test_silo.py), [src/pyleans/test/test_silo_management.py](../../src/pyleans/test/test_silo_management.py), [src/pyleans/test/test_gateway.py](../../src/pyleans/test/test_gateway.py), [src/pyleans/test/test_string_cache_grain.py](../../src/pyleans/test/test_string_cache_grain.py), [src/counter_app/test/test_counter_grain.py](../../src/counter_app/test/test_counter_grain.py), [src/counter_client/test/test_counter_client.py](../../src/counter_client/test/test_counter_client.py) — every `FakeMembershipProvider` replaced with `pyleans.testing.InMemoryMembershipProvider`; assertions that poked the legacy `.silos` dict or `heartbeat_count` were rewritten against `read_all()`.
- [.claude/coding-rules.md](../../.claude/coding-rules.md) — added Rule 10 "Remove Legacy Code": migrations must land with the call-site updates, not with compatibility shims.

### Key decisions

- **Legacy surface removed, not wrapped.** The ABC exposes exactly the OCC primitives. Silo, counter_app, and every test use the primitives directly; there is one way to mutate the membership table.
- **One shared in-memory test double at `pyleans.testing.InMemoryMembershipProvider`** — not six copies of the same dict-backed fake scattered across test roots. The fake implements the OCC contract so tests exercise the same code path as production adapters. Observation attributes (`silos`, `heartbeat_count`, `version`) are exposed as properties rather than public dicts.
- **Silo moves all state transitions into the OCC read-modify-write pattern.** `_bump_heartbeat`, `_transition_status`, `_unregister_from_membership`, `_occ_write` each retry up to `_OCC_MAX_RETRIES=5` times on `TableStaleError` before giving up. The retry loop is the only place outside the provider implementations that touches OCC; everywhere else calls Silo.
- **`try_delete_silo` is the OCC delete primitive on the ABC** — no separate `unregister` wrapper. Providers that can physically remove rows (YAML, Markdown) implement it; providers that can't (a hypothetical append-only log) would raise `NotImplementedError` at call time, which is loud and diagnosable.
- **Silo canonical id changed from `.encoded` to `.silo_id`.** The Phase 2 protocol (hash ring, directory, failure detector, handshake) uses `host:port:epoch`; continuing to use the underscored `host_port_epoch` form in the silo would split the address namespace.
- **ETag format `h-<12-hex-chars-of-SHA256-digest>`** keyed by `version::canonical-payload`. Collision resistance is ample at PoC cluster sizes; the version prefix guarantees an etag changes even if two writers regenerate a row identical to a prior state.
- **`dataclasses.replace` everywhere** via the shared `with_replacements` helper.

### Deviations

None. The ABC now matches the task spec's Phase 2 contract exactly, with the deliberate choice to *not* keep the Phase 1 convenience methods as default implementations (per the new coding rule on legacy removal).

### Test coverage summary

26 new parameterised OCC tests in `test_membership_occ.py`:

- **Snapshot** (3): empty → version 0; first write → version bump; rows carry etags.
- **try_update_silo** (4): create with `etag=None`; create with non-`None` etag → `SiloNotFoundError`; update with matching etag; update with stale etag → `TableStaleError`.
- **try_add_suspicion** (4): happy-path append; stale-etag rejection; unknown silo rejection; 5 concurrent appenders serialise through OCC (all 5 votes appear, distinct ids).
- **Phase 2 fields** (2): cluster_id and gateway_port persist through a write/read round-trip; i_am_alive round-trips.
- **Parametrisation guard** (1): both providers are covered.

File-format tests: rewritten — 9 YAML tests (file creation, Phase 2 fields, multi-silo, active filter, empty snapshot, version bump, physical delete, YAML validity) + 7 Markdown tests (file creation, header, row-per-silo, pipe/newline rejection, malformed row, blank-line parsing).

Full suite: **572 tests** pass; `ruff check`, `ruff format --check`, `pylint` 10.00/10, `mypy` strict clean.
