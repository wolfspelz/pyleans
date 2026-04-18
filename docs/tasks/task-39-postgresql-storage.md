# Task 39: PostgreSQL Storage Provider

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-05-provider-abcs.md](task-05-provider-abcs.md)
- [task-11-file-storage.md](task-11-file-storage.md) (reference -- same ABC, different backend)
- [task-38-postgresql-membership.md](task-38-postgresql-membership.md) (shares the connection-pool and schema-migration approach)

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

PostgreSQL delivers all four, shares the deployment with the PostgreSQL membership provider from [task-38](task-38-postgresql-membership.md), and keeps the production story to a single stateful dependency.

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

JSONB over plain TEXT: gives us server-side JSON validation (malformed state rejected at write time, not read time), cheap attribute access for future admin tooling (`SELECT state->>'value' FROM ...`), and slightly smaller on-disk representation for typical grain payloads. The provider serializes via the existing `JsonSerializer` ([task-04](task-04-serialization.md)) and hands a `jsonb`-compatible string to asyncpg.

Composite primary key `(grain_type, grain_key)` is already the natural unique constraint for the ABC. No separate index needed -- the PK index handles every lookup.

### Operations

Maps the `StorageProvider` ABC contract from [task-05](task-05-provider-abcs.md) to SQL:

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

Grains today serialize via `JsonSerializer` ([task-04](task-04-serialization.md)), so JSONB fits naturally. If a future grain needs binary-only state (e.g. raw protobuf), this provider is not the right home -- add a separate `PostgreSQLBytesStorageProvider` at that point rather than degrading the JSONB design with Python-side base64 wrappers. YAGNI until the requirement materializes.

### Dependencies

Reuses the `postgresql` optional extra introduced in [task-38](task-38-postgresql-membership.md). No new dependency.

### Error mapping

| PostgreSQL / asyncpg exception | Raised as |
|---|---|
| `asyncpg.PostgresConnectionError`, `asyncpg.CannotConnectNowError` | `StorageUnavailableError` (new subclass of `StorageError`) |
| Empty `UPDATE ... RETURNING` or `DELETE 0` | `StorageInconsistencyError` (matches ABC contract) |
| `asyncpg.UniqueViolationError` on insert | `StorageInconsistencyError` |
| `asyncpg.DataError` on malformed JSON | `StorageSerializationError` |
| Deadlocks / serialization errors | retried transparently up to 3 times, then `StorageError` |

Grain runtime ([task-08](task-08-grain-runtime.md)) distinguishes `StorageInconsistencyError` (grain code should handle via retry or conflict resolution) from `StorageUnavailableError` (infrastructure — silo logs WARN, grain can still proceed with cached state until storage returns).

### Retention and cleanup

Out of scope for this task. Grains that have been removed from the cluster (logically deleted by `clear()`) leave no rows behind. Grains that have been idle for long periods still have rows; that's expected behavior — the state lives until an explicit `clear()`. A future admin task (post-PoC) can surface "orphaned" rows whose grain_type is no longer registered.

### Acceptance criteria

- [ ] Schema creates via `auto_migrate=True`; idempotent
- [ ] `read` returns `({}, None)` for absent grains, `(state, etag)` for present ones
- [ ] `write(..., expected_etag=None)` inserts; second insert raises `StorageInconsistencyError`
- [ ] `write(..., expected_etag=correct)` updates; etag monotonically increments
- [ ] `write(..., expected_etag=stale)` raises `StorageInconsistencyError`, row unchanged
- [ ] `clear(..., expected_etag=None)` deletes unconditionally (idempotent on missing rows)
- [ ] `clear(..., expected_etag=stale)` raises `StorageInconsistencyError`
- [ ] Concurrent writes to different grains succeed in parallel (no global lock)
- [ ] Concurrent writes to the same grain with the same expected etag: exactly one succeeds, the other(s) raise `StorageInconsistencyError`
- [ ] Shared pool with `PostgreSQLMembershipProvider` works; neither provider starves the other under load
- [ ] Connection loss surfaces as `StorageUnavailableError`; pool recovers without silo restart
- [ ] State round-trips arbitrary JSON-serializable dicts (via `JsonSerializer` compatibility)
- [ ] Unit tests against `pytest-postgresql`; integration tests against a containerized Postgres behind `@pytest.mark.integration`
- [ ] `ruff`, `pylint` (10.00/10), `mypy` all clean

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
