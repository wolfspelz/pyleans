"""Shared test fixtures and helpers for pyleans tests."""

import time
from typing import Any

from pyleans.providers.storage import StorageProvider


class FakeStorageProvider(StorageProvider):
    """In-memory storage for testing."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict[str, Any], str]] = {}

    async def read(self, grain_type: str, grain_key: str) -> tuple[dict[str, Any], str | None]:
        key = f"{grain_type}/{grain_key}"
        if key in self._store:
            state, etag = self._store[key]
            return state, etag
        return {}, None

    async def write(
        self,
        grain_type: str,
        grain_key: str,
        state: dict[str, Any],
        expected_etag: str | None,
    ) -> str:
        key = f"{grain_type}/{grain_key}"
        new_etag = str(time.monotonic())
        self._store[key] = (state, new_etag)
        return new_etag

    async def clear(
        self,
        grain_type: str,
        grain_key: str,
        expected_etag: str | None,
    ) -> None:
        key = f"{grain_type}/{grain_key}"
        self._store.pop(key, None)
