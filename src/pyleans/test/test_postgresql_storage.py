"""Unit tests for :class:`PostgreSQLStorageProvider`.

Covers construction validation, lazy-import error path, and the
pure-Python JSON codec helpers. Live-database tests are deferred
until CI has a PostgreSQL service (dev env lacks asyncpg).
"""

from __future__ import annotations

import pytest
from pyleans.errors import StorageSerializationError
from pyleans.server.providers.postgresql_storage import (
    PostgreSQLStorageProvider,
    _load_asyncpg,
)


class TestConstruction:
    def test_requires_dsn_or_pool(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="dsn or pool"):
            PostgreSQLStorageProvider()

    def test_rejects_both_dsn_and_pool(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="mutually exclusive"):
            PostgreSQLStorageProvider(dsn="postgres://", pool=object())

    def test_rejects_inverted_pool_sizes(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="max_pool_size"):
            PostgreSQLStorageProvider(dsn="postgres://", min_pool_size=5, max_pool_size=2)


class TestMissingAsyncpg:
    def test_load_asyncpg_raises_clear_error(self) -> None:
        # Arrange
        import sys

        saved = sys.modules.pop("asyncpg", None)
        try:
            # Act / Assert
            with pytest.raises(ImportError, match="postgresql"):
                _load_asyncpg()
        finally:
            if saved is not None:
                sys.modules["asyncpg"] = saved


class TestStateCodec:
    def test_encode_roundtrips_simple_dict(self) -> None:
        # Arrange
        state = {"counter": 42, "name": "grain-1"}

        # Act
        encoded = PostgreSQLStorageProvider._encode_state(state)  # type: ignore[misc]
        decoded = PostgreSQLStorageProvider._decode_state(encoded)  # type: ignore[misc]

        # Assert
        assert decoded == state

    def test_decode_accepts_dict_directly(self) -> None:
        # Arrange - asyncpg with JSONB codec returns python dicts
        state = {"a": 1}

        # Act
        decoded = PostgreSQLStorageProvider._decode_state(state)  # type: ignore[misc]

        # Assert
        assert decoded == state
        assert decoded is not state  # must be a copy so callers can mutate

    def test_encode_rejects_non_serialisable_state(self) -> None:
        # Arrange
        unserialisable = {"bad": object()}

        # Act / Assert
        with pytest.raises(StorageSerializationError):
            PostgreSQLStorageProvider._encode_state(unserialisable)  # type: ignore[misc]

    def test_decode_rejects_non_object_json(self) -> None:
        # Act / Assert
        with pytest.raises(StorageSerializationError):
            PostgreSQLStorageProvider._decode_state("[1, 2, 3]")  # type: ignore[misc]

    def test_decode_rejects_malformed_json(self) -> None:
        # Act / Assert
        with pytest.raises(StorageSerializationError):
            PostgreSQLStorageProvider._decode_state("{not valid json")  # type: ignore[misc]

    def test_decode_rejects_unknown_type(self) -> None:
        # Act / Assert
        with pytest.raises(StorageSerializationError):
            PostgreSQLStorageProvider._decode_state(42)  # type: ignore[misc]
