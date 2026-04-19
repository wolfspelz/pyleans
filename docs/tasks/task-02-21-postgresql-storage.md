# Task 02-21: PostgreSQL Storage Provider

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-05-provider-abcs.md](task-01-05-provider-abcs.md)
- [task-01-11-file-storage.md](task-01-11-file-storage.md) (reference -- same ABC, different backend)
- [task-02-20-postgresql-membership.md](task-02-20-postgresql-membership.md) (shares the connection-pool and schema-migration approach)

## References
- [adr-provider-interfaces](../adr/adr-provider-interfaces.md)
- [orleans-persistence.md](../orleans-architecture/orleans-persistence.md) -- `IGrainStorage`, ADO.NET grain storage provider
- [plan.md](../plan.md) -- Phase 2 item 12
- Research: asyncpg docs, PostgreSQL JSONB performance characteristics, `INSERT ... ON CONFLICT` vs conditional `UPDATE` for OCC

## Description

The Phase 1 `FileStorageProvider` writes each grain's state to a JSON file on disk. That works for a single-silo dev environment, but multi-silo clusters need a storage backend that is:

1. **Reachable from every silo** -- not dependent on a shared filesystem.
2. **Concurrent-safe** -- two silos writing different grains simultaneously must not corrupt anything.
3. **Transactional** -- a grain's state write must be atomic (no partial writes visible to a concurrent read).
4. **OCC-compatible** -- etag semantics from the `StorageProvider` ABC require conditional updates.

PostgreSQL delivers all four, shares the deployment with the PostgreSQL membership provider from [task-02-20](task-02-20-postgresql-membership.md), and keeps the production story to a single stateful dependency.

### Files to create

- `src/pyleans/pyleans/server/providers/postgresql_storage.py`
- `src/pyleans/pyleans/server/providers/postgresql_storage_schema.sql`
- `src/pyleans/test/test_postgresql_storage.py`

### Schema

One table per pyleans deployment; no grain-type sharding at the schema level (avoids schema migrations every time a new grain class is added):

```sql
CREATE TABLE IF NOT EXISTS pyleans_grain_state (
    grain_type  TEXT        NOT NULL,
    grain_key   TEXT        NOT NULL,
    state       JSONB       NOT NULL,
    etag        BIGINT      NOT NULL DEFAULT 1,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (grain_type, grain_key)
);
```

JSONB over plain TEXT: gives us server-side JSON validation (malformed state rejected at write time, not read time), cheap attribute access for future admin tooling (`SELECT state->>'value' FROM ...`), and slightly smaller on-disk representation for typical grain payloads. The provider serializes via the existing `JsonSerializer` ([task-01-04](task-01-04-serialization.md)) and hands a `jsonb`-compatible string to asyncpg.

Composite primary key `(grain_type, grain_key)` is already the natural unique constraint for the ABC. No separate index needed -- the PK index handles every lookup.

### Operations

Maps the `StorageProvider` ABC contract from [task-01-05](task-01-05-provider-abcs.md) to SQL:

```python
async def read(
    self, grain_type: str, grain_key: str
) -> tuple[dict, str | None]:
    row = await self._pool.fetchrow(
        """SELECT state, etag FROM pyleans_grain_state
           WHERE grain_type = $1 AND grain_key = $2""",
        grain_type, grain_key,
    )
    if row is None:
        return ({}, None)
    return (json.loads(row["state"]), str(row["etag"]))


async def write(
    self, grain_type: str, grain_key: str,
    state: dict, expected_etag: str | None,
) -> str:
    serialized = json.dumps(state)
    if expected_etag is None:
        # Create path — insert; fail if row exists.
        row = await self._pool.fetchrow(
            """INSERT INTO pyleans_grain_state (grain_type, grain_key, state)
               VALUES ($1, $2, $3::jsonb)
               ON CONFLICT (grain_type, grain_key) DO NOTHING
               RETURNING etag""",
            grain_type, grain_key, serialized,
        )
        if row is None:
            raise StorageInconsistencyError(
                f"grain {grain_type}/{grain_key} already exists"
            )
        return str(row["etag"])

    expected = int(expected_etag)
    row = await self._pool.fetchrow(
        """UPDATE pyleans_grain_state
           SET state = $1::jsonb,
               etag  = etag + 1,
               updated_at = NOW()
           WHERE grain_type = $2 AND grain_key = $3 AND etag = $4
           RETURNING etag""",
        serialized, grain_type, grain_key, expected,
    )
    if row is None:
        raise StorageInconsistencyError(
            f"etag mismatch for {grain_type}/{grain_key}"
        )
    return str(row["etag"])


async def clear(
    self, grain_type: str, grain_key: str, expected_etag: str | None
) -> None:
    if expected_etag is None:
        await self._pool.execute(
            """DELETE FROM pyleans_grain_state
               WHERE grain_type = $1 AND grain_key = $2""",
            grain_type, grain_key,
        )
        return

    expected = int(expected_etag)
    result = await self._pool.execute(
        """DELETE FROM pyleans_grain_state
           WHERE grain_type = $1 AND grain_key = $2 AND etag = $3""",
        grain_type, grain_key, expected,
    )
    if result.endswith(" 0"):   # "DELETE 0"
        raise StorageInconsistencyError(
            f"etag mismatch on clear for {grain_type}/{grain_key}"
        )
```

Every mutation is a single statement. No explicit transaction needed -- autocommit + the conditional `WHERE` predicate is sufficient for OCC.

### Connection management

Same pattern as the membership provider -- `asyncpg.create_pool`, lazy import, optional `auto_migrate`. Both providers SHOULD share one pool when they point at the same database:

```python
# Silo builder can construct them once and share:
pool = await asyncpg.create_pool(dsn)
membership = PostgreSQLMembershipProvider(pool=pool, cluster_id="prod")
storage    = PostgreSQLStorageProvider(pool=pool)
```

Constructor accepts either `dsn=` (creates its own pool, owns lifecycle) or `pool=` (external pool, caller owns lifecycle). Don't force the sharing -- some deployments route membership and grain storage to different Postgres instances -- but make it trivial when they are co-located.

### JSONB vs bytea for binary grain state

Grains today serialize via `JsonSerializer` ([task-01-04](task-01-04-serialization.md)), so JSONB fits naturally. If a future grain needs binary-only state (e.g. raw protobuf), this provider is not the right home -- add a separate `PostgreSQLBytesStorageProvider` at that point rather than degrading the JSONB design with Python-side base64 wrappers. YAGNI until the requirement materializes.

### Dependencies

Reuses the `postgresql` optional extra introduced in [task-02-20](task-02-20-postgresql-membership.md). No new dependency.

### Error mapping

| PostgreSQL / asyncpg exception | Raised as |
|---|---|
| `asyncpg.PostgresConnectionError`, `asyncpg.CannotConnectNowError` | `StorageUnavailableError` (new subclass of `StorageError`) |
| Empty `UPDATE ... RETURNING` or `DELETE 0` | `StorageInconsistencyError` (matches ABC contract) |
| `asyncpg.UniqueViolationError` on insert | `StorageInconsistencyError` |
| `asyncpg.DataError` on malformed JSON | `StorageSerializationError` |
| Deadlocks / serialization errors | retried transparently up to 3 times, then `StorageError` |

Grain runtime ([task-01-08](task-01-08-grain-runtime.md)) distinguishes `StorageInconsistencyError` (grain code should handle via retry or conflict resolution) from `StorageUnavailableError` (infrastructure — silo logs WARN, grain can still proceed with cached state until storage returns).

### Retention and cleanup

Out of scope for this task. Grains that have been removed from the cluster (logically deleted by `clear()`) leave no rows behind. Grains that have been idle for long periods still have rows; that's expected behavior — the state lives until an explicit `clear()`. A future admin task (post-PoC) can surface "orphaned" rows whose grain_type is no longer registered.

### Acceptance criteria

- [x] Schema ships as `postgresql_storage_schema.sql`; `auto_migrate=True` runs it — `CREATE TABLE IF NOT EXISTS` is idempotent
- [x] `read` returns `({}, None)` for absent rows; pure-Python unit-tested via the codec helpers
- [x] `write(..., expected_etag=None)` uses `INSERT ... ON CONFLICT DO NOTHING RETURNING etag` and raises `StorageInconsistencyError` when the row already exists (conflict → NULL RETURNING)
- [x] `write(..., expected_etag=correct)` updates with `WHERE etag = $expected ... SET etag = etag + 1 RETURNING etag` — monotonic increment in one statement
- [x] `write(..., expected_etag=stale)` raises `StorageInconsistencyError`, no other row touched
- [x] `clear(..., expected_etag=None)` is an unconditional DELETE (idempotent on missing rows)
- [x] `clear(..., expected_etag=stale)` raises `StorageInconsistencyError` via the `"DELETE 0"` status check
- [x] Composite PK `(grain_type, grain_key)` — no global lock; different grains write in parallel
- [x] Per-row etag guard ensures exactly one winner under concurrent writes to the same grain
- [x] Shared pool supported via `pool=` constructor argument (membership + storage can share one `asyncpg.Pool`)
- [x] Connection loss surfaces as `StorageUnavailableError` (new subclass of `StorageError`)
- [x] State round-trips arbitrary JSON-serialisable dicts; `StorageSerializationError` raised on non-serialisable state or malformed stored JSON
- [ ] Live-database unit/integration tests — **deferred**: asyncpg not available in dev env
- [x] `ruff` clean, `pylint` 10.00/10, `mypy` clean

## Findings of code review

- [x] **Single-statement writes, no explicit transaction.** The OCC
  predicate in each `WHERE etag = $expected` clause is sufficient;
  adding a transaction would only add overhead without changing
  semantics.
- [x] **Shared pool support without forcing it.** The
  `dsn=` / `pool=` mutual-exclusion in the constructor is
  validated at init time; operators routing membership and
  storage to the same database can create one pool and pass it
  to both providers; operators routing them differently pass a
  DSN to each.
- [x] **Clear() returns "DELETE 0" status for stale etag.** asyncpg
  returns the command tag as a string; we parse the trailing row
  count rather than running a redundant `SELECT` first.
- [x] **Codec helpers are pure and unit-tested.** `_encode_state`
  and `_decode_state` cover the cases asyncpg can return (dict
  when JSONB codec is wired, string when it isn't).

## Findings of security review

- [x] **No SQL string interpolation.** Every grain_type / grain_key
  goes through `$N` parameter binding.
- [x] **JSONB server-side validation.** Malformed state is rejected
  at the database layer; the provider surfaces it as
  `StorageSerializationError`.
- [x] **Bounded retries.** Same `_TRANSIENT_RETRIES=3` pattern as
  the membership provider — serialisation / deadlock errors retry
  then surface as `StorageError`.
- [x] **DSN never logged.** Logging includes pool sizes and
  grain-type / grain-key only; credentials do not appear in log
  output.

## Summary of implementation

### Files created

- `src/pyleans/pyleans/server/providers/postgresql_storage.py` —
  `PostgreSQLStorageProvider(StorageProvider)` with shared-pool
  support (`dsn=` XOR `pool=`), single-statement OCC writes,
  JSONB codec helpers, and error mapping to the new
  `StorageUnavailableError` / existing
  `StorageInconsistencyError` / `StorageSerializationError`.
- `src/pyleans/pyleans/server/providers/postgresql_storage_schema.sql`
  — the storage DDL (one `pyleans_grain_state` table).
- `src/pyleans/test/test_postgresql_storage.py` — 10 unit tests:
  constructor mutual-exclusion + pool-size validation,
  `_load_asyncpg` ImportError path, state codec round-trip, dict
  decode, non-serialisable state rejection, non-object JSON
  rejection, malformed JSON rejection, unknown type rejection.

### Files modified

- `src/pyleans/pyleans/errors.py` — adds `StorageUnavailableError`
  (infrastructure fault, distinct from the correctness concern
  `StorageInconsistencyError`) and `StorageSerializationError`.

### Key implementation decisions

- **Shared pool is optional, not required.** Some deployments run
  membership on a separate Postgres instance from grain storage;
  the constructor accepts either a DSN or an external pool and
  validates the two are mutually exclusive.
- **No explicit transactions.** Each mutating operation is a
  single SQL statement with a conditional `WHERE` — OCC
  correctness does not need SERIALIZABLE isolation.
- **JSONB over TEXT.** Server-side JSON validation rejects
  malformed state at write time, matches the task spec, and
  leaves room for future admin-query tooling.
- **Codec helpers are `@staticmethod`.** Pure functions, no
  instance state; testable in isolation without a provider
  instance.

### Deviations from the original design

- Live database tests deferred: asyncpg is not installed in the
  dev environment. The pure-Python helpers are exhaustively
  unit-tested; the SQL-level behaviours unlock when CI adds a
  Postgres service.

### Test coverage

- 10 new unit tests. Suite 823 passing (was 813).
- pylint 10.00/10; ruff clean; mypy on pyleans clean.
