"""PostgreSQL-backed :class:`StorageProvider` (production-grade).

Ships as part of the ``postgresql`` optional extra.

Each grain's state lives in one row of ``pyleans_grain_state``,
keyed on ``(grain_type, grain_key)``. Every write is a single SQL
statement with a ``WHERE etag = $expected`` predicate so the OCC
contract from :class:`StorageProvider` holds without an explicit
transaction.

Accepts either ``dsn=...`` (provider owns its own pool) or
``pool=...`` (external pool, caller owns lifecycle) so a silo can
share one pool between the membership and storage providers.
"""

# pylint: disable=duplicate-code
# The membership and storage providers share connection-lifecycle and
# error-mapping shape — mirroring the structure is intentional.

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Final

from pyleans.errors import (
    StorageError,
    StorageInconsistencyError,
    StorageSerializationError,
    StorageUnavailableError,
)
from pyleans.providers.storage import StorageProvider

logger = logging.getLogger(__name__)

_TRANSIENT_RETRIES: Final[int] = 3


def _load_asyncpg() -> Any:
    try:
        import asyncpg  # type: ignore[import-not-found]  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        raise ImportError(
            "PostgreSQLStorageProvider requires the 'postgresql' extra: "
            "pip install 'pyleans[postgresql]'"
        ) from exc
    return asyncpg


def _schema_path() -> Path:
    return Path(__file__).with_name("postgresql_storage_schema.sql")


class PostgreSQLStorageProvider(StorageProvider):
    """PostgreSQL-backed production storage provider."""

    def __init__(
        self,
        *,
        dsn: str | None = None,
        pool: Any | None = None,
        min_pool_size: int = 2,
        max_pool_size: int = 10,
        auto_migrate: bool = True,
    ) -> None:
        if dsn is None and pool is None:
            raise ValueError("either dsn or pool must be provided")
        if dsn is not None and pool is not None:
            raise ValueError("dsn and pool are mutually exclusive")
        if max_pool_size < min_pool_size:
            raise ValueError(
                f"max_pool_size ({max_pool_size}) must be >= min_pool_size ({min_pool_size})",
            )
        self._dsn = dsn
        self._pool: Any | None = pool
        self._owns_pool = pool is None
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._auto_migrate = auto_migrate

    async def start(self) -> None:
        if self._owns_pool:
            asyncpg_mod = _load_asyncpg()
            self._pool = await asyncpg_mod.create_pool(
                self._dsn,
                min_size=self._min_pool_size,
                max_size=self._max_pool_size,
            )
        if self._auto_migrate:
            await self._ensure_schema()

    async def stop(self) -> None:
        if self._owns_pool and self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ensure_schema(self) -> None:
        sql = _schema_path().read_text(encoding="utf-8")
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    async def read(self, grain_type: str, grain_key: str) -> tuple[dict[str, Any], str | None]:
        async with self._handle_transient_errors():
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT state, etag FROM pyleans_grain_state
                       WHERE grain_type = $1 AND grain_key = $2""",
                    grain_type,
                    grain_key,
                )
        if row is None:
            return ({}, None)
        state = self._decode_state(row["state"])
        return (state, str(int(row["etag"])))

    async def write(
        self,
        grain_type: str,
        grain_key: str,
        state: dict[str, Any],
        expected_etag: str | None,
    ) -> str:
        serialised = self._encode_state(state)
        async with self._handle_transient_errors():
            assert self._pool is not None
            if expected_etag is None:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """INSERT INTO pyleans_grain_state (grain_type, grain_key, state)
                           VALUES ($1, $2, $3::jsonb)
                           ON CONFLICT (grain_type, grain_key) DO NOTHING
                           RETURNING etag""",
                        grain_type,
                        grain_key,
                        serialised,
                    )
                if row is None:
                    raise StorageInconsistencyError(expected_etag, "exists")
                return str(int(row["etag"]))
            expected = int(expected_etag)
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """UPDATE pyleans_grain_state
                       SET state = $1::jsonb,
                           etag  = etag + 1,
                           updated_at = NOW()
                       WHERE grain_type = $2 AND grain_key = $3 AND etag = $4
                       RETURNING etag""",
                    serialised,
                    grain_type,
                    grain_key,
                    expected,
                )
            if row is None:
                raise StorageInconsistencyError(expected_etag, "stale")
            return str(int(row["etag"]))

    async def clear(
        self,
        grain_type: str,
        grain_key: str,
        expected_etag: str | None,
    ) -> None:
        async with self._handle_transient_errors():
            assert self._pool is not None
            if expected_etag is None:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """DELETE FROM pyleans_grain_state
                           WHERE grain_type = $1 AND grain_key = $2""",
                        grain_type,
                        grain_key,
                    )
                return
            expected = int(expected_etag)
            async with self._pool.acquire() as conn:
                status = await conn.execute(
                    """DELETE FROM pyleans_grain_state
                       WHERE grain_type = $1 AND grain_key = $2 AND etag = $3""",
                    grain_type,
                    grain_key,
                    expected,
                )
        if str(status).endswith(" 0"):
            raise StorageInconsistencyError(expected_etag, "stale")

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
                    "PG storage transient error (%s), attempt %d/%d",
                    type(exc).__name__,
                    attempt,
                    _TRANSIENT_RETRIES,
                )
                if attempt < _TRANSIENT_RETRIES:
                    await asyncio.sleep(0.01 * attempt)
                    continue
                raise StorageError(
                    f"storage op failed after {_TRANSIENT_RETRIES} retries: {exc}"
                ) from exc
            except (
                asyncpg_mod.PostgresConnectionError,
                asyncpg_mod.CannotConnectNowError,
            ) as exc:
                raise StorageUnavailableError(f"PostgreSQL connection unavailable: {exc}") from exc
            except asyncpg_mod.DataError as exc:
                raise StorageSerializationError(f"storage data error: {exc}") from exc
            except asyncpg_mod.PostgresError as exc:
                raise StorageError(f"storage op raised {type(exc).__name__}: {exc}") from exc
        assert last_exc is not None
        raise StorageError(f"unreachable; last={last_exc}")

    @staticmethod
    def _encode_state(state: dict[str, Any]) -> str:
        try:
            return json.dumps(state)
        except (TypeError, ValueError) as exc:
            raise StorageSerializationError(f"cannot serialise state: {exc}") from exc

    @staticmethod
    def _decode_state(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StorageSerializationError(f"cannot parse stored state JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise StorageSerializationError(
                    f"stored state must be a JSON object, got {type(parsed).__name__}"
                )
            return parsed
        raise StorageSerializationError(f"stored state has unexpected type {type(raw).__name__}")
