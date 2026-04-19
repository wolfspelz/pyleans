"""PostgreSQL-backed :class:`MembershipProvider` (production-grade).

Ships as an optional extra: install with ``pip install 'pyleans[postgresql]'``.

Key properties:

* Per-row optimistic concurrency via an integer ``etag_version`` column —
  every write runs under ``WHERE etag_version = $expected`` so losers
  see an empty ``RETURNING`` and raise :class:`TableStaleError`.
* Atomic ``version`` counter in a separate :class:`pyleans_membership_version`
  row, incremented in the same transaction as every mutating write.
* Optional ``LISTEN``/``NOTIFY`` pub/sub for low-latency snapshot
  broadcasts; polling is the correctness fallback.
* Connection-loss surfaces as :class:`MembershipUnavailableError` so the
  failure detector pauses voting instead of escalating.

:mod:`asyncpg` is imported lazily so installations without the
PostgreSQL extra still import the module without error.
"""

# pylint: disable=duplicate-code
# The YAML, Markdown, and PostgreSQL providers share per-silo row
# coercion boilerplate — mirroring the shape is intentional.

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pyleans.errors import MembershipError
from pyleans.identity import SiloAddress, SiloInfo, SiloStatus, SuspicionVote
from pyleans.providers.errors import (
    MembershipUnavailableError,
    TableStaleError,
)
from pyleans.providers.membership import MembershipProvider, MembershipSnapshot

logger = logging.getLogger(__name__)

_NOTIFY_CHANNEL: Final[str] = "pyleans_membership_changes"
_TRANSIENT_RETRIES: Final[int] = 3


def _load_asyncpg() -> Any:
    try:
        import asyncpg  # type: ignore[import-not-found]  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        raise ImportError(
            "PostgreSQLMembershipProvider requires the 'postgresql' extra: "
            "pip install 'pyleans[postgresql]'"
        ) from exc
    return asyncpg


def _schema_path() -> Path:
    return Path(__file__).with_name("postgresql_schema.sql")


class PostgreSQLMembershipProvider(MembershipProvider):
    """PostgreSQL-backed production membership provider."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        dsn: str,
        cluster_id: str,
        *,
        min_pool_size: int = 2,
        max_pool_size: int = 10,
        pubsub_enabled: bool = True,
        auto_migrate: bool = True,
    ) -> None:
        if max_pool_size < min_pool_size:
            raise ValueError(
                f"max_pool_size ({max_pool_size}) must be >= min_pool_size ({min_pool_size})",
            )
        self._dsn = dsn
        self._cluster_id = cluster_id
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._pubsub_enabled = pubsub_enabled
        self._auto_migrate = auto_migrate
        self._pool: Any | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._subscribers: list[asyncio.Queue[int]] = []

    @property
    def cluster_id(self) -> str:
        return self._cluster_id

    async def start(self) -> None:
        asyncpg_mod = _load_asyncpg()
        self._pool = await asyncpg_mod.create_pool(
            self._dsn,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
        )
        if self._auto_migrate:
            await self._ensure_schema()
        if self._pubsub_enabled:
            self._listener_task = asyncio.create_task(
                self._listen_loop(), name="pyleans-membership-listen"
            )

    async def stop(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def subscribe_notifications(self) -> asyncio.Queue[int]:
        """Return a queue that receives the ``version`` of every change event."""
        queue: asyncio.Queue[int] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    async def _ensure_schema(self) -> None:
        sql = _schema_path().read_text(encoding="utf-8")
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def read_all(self) -> MembershipSnapshot:
        async with self._handle_transient_errors():
            assert self._pool is not None
            async with (
                self._pool.acquire() as conn,
                conn.transaction(isolation="read_committed"),
            ):
                rows = await conn.fetch(
                    """SELECT * FROM pyleans_membership WHERE cluster_id = $1""",
                    self._cluster_id,
                )
                version = await self._read_version(conn)
        silos = [self._row_to_silo(row) for row in rows]
        return MembershipSnapshot(version=version, silos=silos)

    async def try_update_silo(self, silo: SiloInfo) -> SiloInfo:
        async with self._handle_transient_errors():
            assert self._pool is not None
            async with self._pool.acquire() as conn, conn.transaction():
                new_version = await self._upsert_silo(conn, silo)
                await self._bump_version(conn)
                snapshot_version = await self._read_version(conn)
        await self._notify_change(snapshot_version)
        return _with_etag(silo, str(new_version))

    async def try_add_suspicion(
        self, silo_id: str, vote: SuspicionVote, expected_etag: str
    ) -> SiloInfo:
        vote_json = json.dumps(
            [{"suspecting_silo": vote.suspecting_silo, "timestamp": vote.timestamp}]
        )
        expected_version = int(expected_etag)
        async with self._handle_transient_errors():
            assert self._pool is not None
            async with self._pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    """UPDATE pyleans_membership
                           SET suspicions = suspicions || $1::jsonb,
                               etag_version = etag_version + 1
                           WHERE cluster_id = $2 AND silo_id = $3
                             AND etag_version = $4
                           RETURNING *""",
                    vote_json,
                    self._cluster_id,
                    silo_id,
                    expected_version,
                )
                if row is None:
                    raise TableStaleError(f"etag mismatch on suspicion write for {silo_id}")
                await self._bump_version(conn)
                snapshot_version = await self._read_version(conn)
        await self._notify_change(snapshot_version)
        return self._row_to_silo(row)

    async def try_delete_silo(self, silo: SiloInfo) -> None:
        if silo.etag is None:
            return
        expected_version = int(silo.etag)
        async with self._handle_transient_errors():
            assert self._pool is not None
            async with self._pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    """DELETE FROM pyleans_membership
                           WHERE cluster_id = $1 AND silo_id = $2
                             AND etag_version = $3
                           RETURNING silo_id""",
                    self._cluster_id,
                    silo.address.silo_id,
                    expected_version,
                )
                if row is None:
                    raise TableStaleError(f"etag mismatch on delete for {silo.address.silo_id}")
                await self._bump_version(conn)
                snapshot_version = await self._read_version(conn)
        await self._notify_change(snapshot_version)

    async def _upsert_silo(self, conn: Any, silo: SiloInfo) -> int:
        suspicions_json = json.dumps(
            [
                {"suspecting_silo": v.suspecting_silo, "timestamp": v.timestamp}
                for v in silo.suspicions
            ]
        )
        start_time = _ts(silo.start_time)
        last_heartbeat = _ts(silo.last_heartbeat)
        i_am_alive = _ts(silo.i_am_alive if silo.i_am_alive > 0 else silo.last_heartbeat)
        if silo.etag is None:
            asyncpg_mod = _load_asyncpg()
            try:
                row = await conn.fetchrow(
                    """INSERT INTO pyleans_membership
                       (cluster_id, silo_id, host, port, epoch, status,
                        gateway_port, start_time, last_heartbeat, i_am_alive,
                        suspicions, etag_version)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, 1)
                       RETURNING etag_version""",
                    self._cluster_id,
                    silo.address.silo_id,
                    silo.address.host,
                    silo.address.port,
                    silo.address.epoch,
                    silo.status.value,
                    silo.gateway_port,
                    start_time,
                    last_heartbeat,
                    i_am_alive,
                    suspicions_json,
                )
            except asyncpg_mod.UniqueViolationError as exc:
                raise TableStaleError(
                    f"silo {silo.address.silo_id!r} already registered",
                ) from exc
        else:
            expected_version = int(silo.etag)
            row = await conn.fetchrow(
                """UPDATE pyleans_membership
                   SET status = $1,
                       gateway_port = $2,
                       last_heartbeat = $3,
                       i_am_alive = $4,
                       suspicions = $5::jsonb,
                       etag_version = etag_version + 1
                   WHERE cluster_id = $6 AND silo_id = $7 AND etag_version = $8
                   RETURNING etag_version""",
                silo.status.value,
                silo.gateway_port,
                last_heartbeat,
                i_am_alive,
                suspicions_json,
                self._cluster_id,
                silo.address.silo_id,
                expected_version,
            )
            if row is None:
                raise TableStaleError(
                    f"etag mismatch on update for {silo.address.silo_id}",
                )
        return int(row["etag_version"])

    async def _bump_version(self, conn: Any) -> None:
        await conn.execute(
            """INSERT INTO pyleans_membership_version (cluster_id, version)
               VALUES ($1, 1)
               ON CONFLICT (cluster_id)
               DO UPDATE SET version = pyleans_membership_version.version + 1""",
            self._cluster_id,
        )

    async def _read_version(self, conn: Any) -> int:
        row = await conn.fetchrow(
            """SELECT version FROM pyleans_membership_version WHERE cluster_id = $1""",
            self._cluster_id,
        )
        return int(row["version"]) if row is not None else 0

    async def _notify_change(self, version: int) -> None:
        if not self._pubsub_enabled or self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"NOTIFY {_NOTIFY_CHANNEL}, $1",  # pylint: disable=consider-using-f-string
                    f"{self._cluster_id}:{version}",
                )
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("NOTIFY failed (best-effort): %r", exc)

    async def _listen_loop(self) -> None:
        assert self._pool is not None
        try:
            async with self._pool.acquire() as conn:

                def _on_notify(
                    connection: Any,
                    pid: int,
                    channel: str,
                    payload: str,
                ) -> None:
                    del connection, pid, channel
                    parts = payload.split(":", 1)
                    if len(parts) != 2 or parts[0] != self._cluster_id:
                        return
                    try:
                        version = int(parts[1])
                    except ValueError:
                        return
                    for queue in list(self._subscribers):
                        with contextlib.suppress(asyncio.QueueFull):
                            queue.put_nowait(version)

                await conn.add_listener(_NOTIFY_CHANNEL, _on_notify)
                await asyncio.Future()  # block until cancelled
        except Exception as exc:  # pylint: disable=broad-except
            # CancelledError is a BaseException in 3.12+ and bypasses this
            # handler naturally; no explicit re-raise needed.
            logger.warning("Membership LISTEN loop exited: %r", exc)

    def _row_to_silo(self, row: Any) -> SiloInfo:
        suspicions_raw = row["suspicions"] or []
        if isinstance(suspicions_raw, str):
            suspicions_raw = json.loads(suspicions_raw)
        suspicions = [
            SuspicionVote(
                suspecting_silo=str(entry["suspecting_silo"]),
                timestamp=float(entry["timestamp"]),
            )
            for entry in suspicions_raw
        ]
        return SiloInfo(
            address=SiloAddress(
                host=str(row["host"]),
                port=int(row["port"]),
                epoch=int(row["epoch"]),
            ),
            status=SiloStatus(row["status"]),
            last_heartbeat=row["last_heartbeat"].timestamp(),
            start_time=row["start_time"].timestamp(),
            cluster_id=str(row["cluster_id"]),
            gateway_port=int(row["gateway_port"]) if row["gateway_port"] is not None else None,
            i_am_alive=row["i_am_alive"].timestamp(),
            suspicions=suspicions,
            etag=str(int(row["etag_version"])),
        )

    @contextlib.asynccontextmanager
    async def _handle_transient_errors(self):  # type: ignore[no-untyped-def]
        asyncpg_mod = _load_asyncpg()
        last_exc: Exception | None = None
        for attempt in range(1, _TRANSIENT_RETRIES + 1):
            try:
                yield
                return
            except (
                asyncpg_mod.SerializationError,
                asyncpg_mod.DeadlockDetectedError,
            ) as exc:
                last_exc = exc
                logger.info(
                    "PG transient error (%s), attempt %d/%d",
                    type(exc).__name__,
                    attempt,
                    _TRANSIENT_RETRIES,
                )
                if attempt < _TRANSIENT_RETRIES:
                    await asyncio.sleep(0.01 * attempt)
                    continue
                raise MembershipError(
                    f"membership op failed after {_TRANSIENT_RETRIES} retries: {exc}"
                ) from exc
            except (
                asyncpg_mod.PostgresConnectionError,
                asyncpg_mod.CannotConnectNowError,
            ) as exc:
                raise MembershipUnavailableError(
                    f"PostgreSQL connection unavailable: {exc}"
                ) from exc
            except asyncpg_mod.PostgresError as exc:
                raise MembershipError(f"membership op raised {type(exc).__name__}: {exc}") from exc
        assert last_exc is not None
        raise MembershipError(f"unreachable; last={last_exc}")


def _with_etag(silo: SiloInfo, etag: str) -> SiloInfo:
    return SiloInfo(
        address=silo.address,
        status=silo.status,
        last_heartbeat=silo.last_heartbeat,
        start_time=silo.start_time,
        cluster_id=silo.cluster_id,
        gateway_port=silo.gateway_port,
        version=silo.version,
        i_am_alive=silo.i_am_alive,
        suspicions=list(silo.suspicions),
        etag=etag,
    )


def _ts(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC)
