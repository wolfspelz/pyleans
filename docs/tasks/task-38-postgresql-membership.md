# Task 38: PostgreSQL Membership Provider

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-27-membership-table-extensions.md](task-27-membership-table-extensions.md)

## References
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §4.2 consistency requirements, §4.3 ADO.NET providers (PostgreSQL is Orleans' reference SQL backend)
- [plan.md](../plan.md) -- Phase 2 item 11
- Research: asyncpg docs, PostgreSQL `SERIALIZABLE` vs application-level OCC, PostgreSQL `LISTEN`/`NOTIFY`, JSONB atomic append via `jsonb_insert`

## Description

File-based membership providers (YAML, Markdown) are dev-mode only: they assume a shared filesystem, rely on advisory locking that fails silently across NFS mounts, and have no audit trail. Production multi-silo clusters need a real database. PostgreSQL is the production provider pyleans ships — it is universally available, operationally well-understood, and delivers the exact consistency primitives the membership table requires (row-level conditional updates, atomic multi-row transactions, native pub/sub via `LISTEN`/`NOTIFY`).

Silos switch between the dev-mode file provider and PostgreSQL by flipping one config value. Same `MembershipProvider` ABC from [task-27](task-27-membership-table-extensions.md); no downstream code changes.

### Files to create

- `src/pyleans/pyleans/server/providers/postgresql_membership.py`
- `src/pyleans/pyleans/server/providers/postgresql_schema.sql` (also embedded as a Python string for self-contained provisioning)
- `src/pyleans/test/test_postgresql_membership.py` (unit tests against an in-process `pg_temp` or a containerized Postgres under a pytest marker)

### Schema

```sql
CREATE TABLE IF NOT EXISTS pyleans_membership (
    cluster_id      TEXT        NOT NULL,
    silo_id         TEXT        NOT NULL,       -- "host:port:epoch"
    host            TEXT        NOT NULL,
    port            INTEGER     NOT NULL,
    epoch           BIGINT      NOT NULL,
    status          TEXT        NOT NULL,       -- 'joining' | 'active' | 'shutting_down' | 'dead'
    gateway_port    INTEGER,
    start_time      TIMESTAMPTZ NOT NULL,
    last_heartbeat  TIMESTAMPTZ NOT NULL,
    i_am_alive      TIMESTAMPTZ NOT NULL,
    suspicions      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    etag_version    BIGINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (cluster_id, silo_id)
);

CREATE INDEX IF NOT EXISTS pyleans_membership_status_idx
    ON pyleans_membership (cluster_id, status);

CREATE TABLE IF NOT EXISTS pyleans_membership_version (
    cluster_id TEXT PRIMARY KEY,
    version    BIGINT NOT NULL DEFAULT 0
);
```

Two tables rather than one: the version counter is logically a singleton per cluster, and keeping it in its own table avoids any chance of a `SELECT *` scan seeing it as a pseudo-silo row.

### Atomic writes

Every mutating operation opens a transaction, performs an OCC-guarded `UPDATE` on the silo row, and bumps the version row atomically:

```python
async def try_update_silo(self, silo: SiloInfo) -> SiloInfo:
    async with self._pool.acquire() as conn:
        async with conn.transaction():
            if silo.etag is None:
                # Create path — INSERT; fail if row exists.
                try:
                    row = await conn.fetchrow(
                        """INSERT INTO pyleans_membership
                           (cluster_id, silo_id, host, port, epoch, status,
                            gateway_port, start_time, last_heartbeat,
                            i_am_alive, etag_version)
                           VALUES ($1, $2, ..., 1)
                           RETURNING etag_version""",
                        ...
                    )
                except asyncpg.UniqueViolationError as e:
                    raise TableStaleError(...) from e
            else:
                expected = int(silo.etag)
                row = await conn.fetchrow(
                    """UPDATE pyleans_membership
                       SET status = $1, last_heartbeat = $2, i_am_alive = $3,
                           gateway_port = $4, etag_version = etag_version + 1
                       WHERE cluster_id = $5 AND silo_id = $6
                         AND etag_version = $7
                       RETURNING etag_version""",
                    ..., expected,
                )
                if row is None:
                    raise TableStaleError(...)

            await conn.execute(
                """INSERT INTO pyleans_membership_version (cluster_id, version)
                   VALUES ($1, 1)
                   ON CONFLICT (cluster_id)
                   DO UPDATE SET version = pyleans_membership_version.version + 1""",
                cluster_id,
            )
    return silo_with_new_etag(row["etag_version"])
```

Consistency isolation: **READ COMMITTED** is sufficient because every mutation is guarded by the `WHERE etag_version = $expected` predicate. Losers see `row is None` and raise `TableStaleError`. There is no read-modify-write across statements without the conditional guard, so we don't need `REPEATABLE READ` or `SERIALIZABLE`.

### Suspicion appends

`try_add_suspicion` uses JSONB atomic append inside a transaction:

```python
row = await conn.fetchrow(
    """UPDATE pyleans_membership
       SET suspicions = suspicions || $1::jsonb,
           etag_version = etag_version + 1
       WHERE cluster_id = $2 AND silo_id = $3 AND etag_version = $4
       RETURNING etag_version""",
    json.dumps([vote_dict]), cluster_id, silo_id, expected_etag,
)
```

Concurrent appenders race on `etag_version`; losers get `TableStaleError` and retry. The JSONB array grows; stale suspicions (older than `death_vote_expiration` per [task-29](task-29-failure-detector.md)) are pruned during the next write.

### `LISTEN`/`NOTIFY` snapshot broadcast

After every successful write, the provider issues `NOTIFY pyleans_membership_changes, '<cluster_id>:<version>'`. Silos subscribe via `LISTEN`:

```python
async def _listen_loop(self) -> None:
    async with self._pool.acquire() as conn:
        await conn.add_listener("pyleans_membership_changes", self._on_change)
        await asyncio.Future()   # block until cancelled
```

This maps naturally onto [task-29 §snapshot broadcast](task-29-failure-detector.md#snapshot-broadcast) — other silos refresh their view without waiting for `table_poll_interval`. `NOTIFY` is best-effort (like Redis pub/sub), so periodic polling remains the correctness fallback.

Optional — controlled by `pubsub_enabled=True` default. Operators that want to minimize the dedicated listener connection can disable it and rely on polling.

### Connection management

```python
class PostgreSQLMembershipProvider(MembershipProvider):
    def __init__(
        self,
        dsn: str,                       # "postgresql://user:pass@host:5432/db"
        cluster_id: str,
        min_pool_size: int = 2,
        max_pool_size: int = 10,
        pubsub_enabled: bool = True,
        auto_migrate: bool = True,
    ) -> None: ...

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
        )
        if self._auto_migrate:
            await self._ensure_schema()
        if self._pubsub_enabled:
            self._listener_task = asyncio.create_task(self._listen_loop())

    async def stop(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
        await self._pool.close()
```

`asyncpg` is the recommended driver — fastest asyncio Postgres driver in the ecosystem, and its prepared-statement cache fits the membership workload (repeated reads of the same 5–10 queries).

`auto_migrate=True` runs `CREATE TABLE IF NOT EXISTS` on startup. Operators who prefer managed migrations disable this and run `postgresql_schema.sql` through their migration tool (Alembic, Flyway, sqitch).

### Dependencies

Optional extra so silos that don't use PostgreSQL don't carry the import cost:

```toml
[project.optional-dependencies]
postgresql = ["asyncpg>=0.29"]
test-postgresql = ["pytest-postgresql>=6.0"]
```

The provider's `__init__` does the `asyncpg` import lazily with a clear error if missing:

```python
try:
    import asyncpg
except ImportError as e:
    raise ImportError(
        "PostgreSQLMembershipProvider requires the 'postgresql' extra: "
        "pip install 'pyleans[postgresql]'"
    ) from e
```

### Error mapping

| PostgreSQL / asyncpg exception | Raised as |
|---|---|
| `asyncpg.PostgresConnectionError`, `asyncpg.CannotConnectNowError` | `MembershipUnavailableError` — failure detector pauses voting |
| `asyncpg.SerializationError`, `asyncpg.DeadlockDetectedError` | retried transparently up to 3 times, then `MembershipError` |
| Empty `UPDATE ... RETURNING` result | `TableStaleError` |
| `asyncpg.UniqueViolationError` on insert | `TableStaleError` (race with another silo registering) |
| Any other `asyncpg.PostgresError` | `MembershipError` with the SQLSTATE code preserved in the message |

### Acceptance criteria

- [ ] Schema creates successfully via `auto_migrate=True`; idempotent (second start is a no-op)
- [ ] `register_silo` inserts a new row, idempotent for the same `silo_id`
- [ ] `try_update_silo` with correct etag succeeds; `etag_version` increments; version row increments
- [ ] `try_update_silo` with stale etag raises `TableStaleError` without modifying any row
- [ ] `try_add_suspicion` is atomic under 20 concurrent tasks — exactly 20 entries land
- [ ] `get_active_silos` uses the status index (verify via `EXPLAIN` in test)
- [ ] `heartbeat` updates `i_am_alive` in a single round-trip
- [ ] `LISTEN`/`NOTIFY` delivers change events to subscribed peers within ~10 ms
- [ ] `pubsub_enabled=False` path works (no listener task; writes don't issue `NOTIFY`)
- [ ] Connection loss surfaces as `MembershipUnavailableError`, not a leaked `asyncpg` exception
- [ ] Connection pool recovers from a broken connection without restart
- [ ] Unit tests against `pytest-postgresql` (in-process test database); integration tests against a containerized Postgres behind `@pytest.mark.integration`
- [ ] `ruff`, `pylint` (10.00/10), `mypy` all clean

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
