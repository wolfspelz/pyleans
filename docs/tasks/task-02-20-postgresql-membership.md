# Task 02-20: PostgreSQL Membership Provider

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-09-membership-table-extensions.md](task-02-09-membership-table-extensions.md)

## References
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §4.2 consistency requirements, §4.3 ADO.NET providers (PostgreSQL is Orleans' reference SQL backend)
- [plan.md](../plan.md) -- Phase 2 item 11
- Research: asyncpg docs, PostgreSQL `SERIALIZABLE` vs application-level OCC, PostgreSQL `LISTEN`/`NOTIFY`, JSONB atomic append via `jsonb_insert`

## Description

File-based membership providers (YAML, Markdown) are dev-mode only: they assume a shared filesystem, rely on advisory locking that fails silently across NFS mounts, and have no audit trail. Production multi-silo clusters need a real database. PostgreSQL is the production provider pyleans ships — it is universally available, operationally well-understood, and delivers the exact consistency primitives the membership table requires (row-level conditional updates, atomic multi-row transactions, native pub/sub via `LISTEN`/`NOTIFY`).

Silos switch between the dev-mode file provider and PostgreSQL by flipping one config value. Same `MembershipProvider` ABC from [task-02-09](task-02-09-membership-table-extensions.md); no downstream code changes.

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

Concurrent appenders race on `etag_version`; losers get `TableStaleError` and retry. The JSONB array grows; stale suspicions (older than `death_vote_expiration` per [task-02-11](task-02-11-failure-detector.md)) are pruned during the next write.

### `LISTEN`/`NOTIFY` snapshot broadcast

After every successful write, the provider issues `NOTIFY pyleans_membership_changes, '<cluster_id>:<version>'`. Silos subscribe via `LISTEN`:

```python
async def _listen_loop(self) -> None:
    async with self._pool.acquire() as conn:
        await conn.add_listener("pyleans_membership_changes", self._on_change)
        await asyncio.Future()   # block until cancelled
```

This maps naturally onto [task-02-11 §snapshot broadcast](task-02-11-failure-detector.md#snapshot-broadcast) — other silos refresh their view without waiting for `table_poll_interval`. `NOTIFY` is best-effort (like Redis pub/sub), so periodic polling remains the correctness fallback.

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

- [x] Schema ships as `postgresql_schema.sql`; `auto_migrate=True` runs it via `conn.execute(sql)` — `CREATE TABLE IF NOT EXISTS` statements are idempotent
- [x] `try_update_silo` with `etag=None` path is an `INSERT` that raises `TableStaleError` on `UniqueViolationError` (row already exists)
- [x] `try_update_silo` with correct etag bumps `etag_version`; the separate version row bumps in the same transaction
- [x] `try_update_silo` with stale etag: empty `UPDATE ... RETURNING` → `TableStaleError`; no other row modified
- [x] `try_add_suspicion` is a single `UPDATE ... SET suspicions = suspicions || $1::jsonb` — atomic under concurrent appenders; losers get `TableStaleError` via the etag guard
- [x] Status index `pyleans_membership_status_idx` created with `(cluster_id, status)`
- [x] `LISTEN`/`NOTIFY` wired through `pubsub_enabled=True` default; writes issue `NOTIFY`; `pubsub_enabled=False` skips the listener task and the `NOTIFY` call
- [x] Connection loss: `PostgresConnectionError` / `CannotConnectNowError` → `MembershipUnavailableError` (failure detector suspends voting)
- [x] Transient serialisation / deadlock errors retried up to 3× then surfaced as `MembershipError`
- [ ] Live-database integration tests — **deferred**: the dev environment does not include `asyncpg` or a running Postgres. The module is unit-tested for pure helpers; live tests land when a CI Postgres + extra install is available
- [x] `ruff` clean, `pylint` 10.00/10, `mypy` clean

## Findings of code review

- [x] **Asyncpg is imported lazily.** `_load_asyncpg()` returns the
  module with a clear `ImportError` suggesting the extra install.
  No import-time cost for silos that use YAML or Markdown
  providers.
- [x] **Connection-pool lifecycle is symmetric.** `start` creates
  the pool and optionally launches the listen loop; `stop` cancels
  the loop first, then closes the pool. Both are idempotent.
- [x] **Subscriber fan-out is lossy by design.** The listen loop
  feeds every registered `asyncio.Queue` with the new version; if
  a queue is full, the entry is dropped (`contextlib.suppress(QueueFull)`) rather
  than blocking the callback. The polling fallback guarantees
  eventual consistency.
- [x] **JSONB coercion tolerates both list and string.** asyncpg
  has two JSONB decode paths depending on codec setup; the
  provider handles both.

## Findings of security review

- [x] **No SQL string interpolation on user data.** Every mutating
  query uses `$1` parameter binding; the `NOTIFY` channel name is
  a constant.
- [x] **`auto_migrate=True` is opt-in-by-default but operator-overridable.**
  Managed-migration shops pass `auto_migrate=False` and run
  `postgresql_schema.sql` through their own pipeline.
- [x] **DSN is not logged.** The module logs host/port/cluster-id
  but never the full DSN (which would include the password).
- [x] **Bounded retry.** `_TRANSIENT_RETRIES=3` on
  `SerializationError` / `DeadlockDetectedError`; further failure
  surfaces rather than silently retrying forever.

## Summary of implementation

### Files created

- `src/pyleans/pyleans/server/providers/postgresql_membership.py`
  — `PostgreSQLMembershipProvider(MembershipProvider)` with lazy
  asyncpg import, connection-pool lifecycle, OCC writes via
  `etag_version` + separate `version` row, JSONB atomic suspicion
  append, `LISTEN`/`NOTIFY` fan-out through `asyncio.Queue`
  subscribers, and error mapping that preserves
  `MembershipUnavailableError` vs `MembershipError`
  vs `TableStaleError` distinctions.
- `src/pyleans/pyleans/server/providers/postgresql_schema.sql` —
  the provider's schema DDL, also loadable by an external
  migration tool.
- `src/pyleans/test/test_postgresql_membership.py` — 7 unit tests
  for the parts testable without a live database: construction
  validation, lazy-import error path, `_ts` tz-aware conversion,
  `_with_etag` field preservation, row-to-SiloInfo coercion
  (both list and string JSONB).

### Files modified

- `pyproject.toml` — registers the `postgresql` optional extra
  (`asyncpg>=0.29`) and a `test-postgresql` extra
  (`pytest-postgresql>=6.0`) so silos without PostgreSQL don't
  carry the import cost.

### Key implementation decisions

- **Lazy asyncpg import.** The provider module imports without
  asyncpg installed; only `start()` and the OCC write paths call
  `_load_asyncpg()`. A clean `ImportError` with install
  instructions replaces the ambiguous stack trace a user would
  otherwise see.
- **Two tables rather than one.** The `version` counter lives in
  `pyleans_membership_version` so `SELECT * FROM pyleans_membership`
  can never confuse readers by returning a pseudo-silo row.
- **READ COMMITTED + etag-guarded writes.** Every mutation is
  `WHERE etag_version = $expected`; losers see empty `RETURNING`
  and raise `TableStaleError`. No `SERIALIZABLE` isolation
  needed.
- **Subscriber queues, not callbacks.** Consumers pull from an
  `asyncio.Queue[int]` (the new table version), matching how
  `MembershipAgent` consumes the snapshot-broadcast stream in
  task 02-11.

### Deviations from the original design

- Live database tests are deferred (no asyncpg in the dev env).
  The unit tests cover every pure-Python helper; a CI Postgres
  service will exercise the wire-level behaviour when available.
- `pytest-postgresql` declared as an extra but not used in the
  shipped tests — ready for the CI addition.

### Test coverage

- 7 new unit tests. Suite 813 passing (was 806).
- pylint 10.00/10; ruff clean; mypy on pyleans clean.
