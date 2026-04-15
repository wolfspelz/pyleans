"""File-based storage provider — one JSON file per grain."""

import uuid
from pathlib import Path
from typing import Any

import orjson

from pyleans.errors import StorageInconsistencyError
from pyleans.providers.storage import StorageProvider


class FileStorageProvider(StorageProvider):
    """Stores grain state as individual JSON files.

    Directory layout: {base_path}/{grain_type}/{safe_key}.json

    Each file contains: {"etag": "uuid-string", "state": {...}}
    """

    def __init__(self, base_path: str = "./pyleans-data/storage") -> None:
        self._base_path = Path(base_path)

    async def read(
        self, grain_type: str, grain_key: str
    ) -> tuple[dict[str, Any], str | None]:
        path = self._grain_path(grain_type, grain_key)
        if not path.exists():
            return {}, None
        data = orjson.loads(path.read_bytes())
        return data["state"], data["etag"]

    async def write(
        self,
        grain_type: str,
        grain_key: str,
        state: dict[str, Any],
        expected_etag: str | None,
    ) -> str:
        path = self._grain_path(grain_type, grain_key)
        if path.exists():
            current = orjson.loads(path.read_bytes())
            if expected_etag is not None and current["etag"] != expected_etag:
                raise StorageInconsistencyError(expected_etag, current["etag"])

        new_etag = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(orjson.dumps({"etag": new_etag, "state": state}))
        return new_etag

    async def clear(
        self,
        grain_type: str,
        grain_key: str,
        expected_etag: str | None,
    ) -> None:
        path = self._grain_path(grain_type, grain_key)
        if not path.exists():
            return
        if expected_etag is not None:
            current = orjson.loads(path.read_bytes())
            if current["etag"] != expected_etag:
                raise StorageInconsistencyError(expected_etag, current["etag"])
        path.unlink()

    def _grain_path(self, grain_type: str, grain_key: str) -> Path:
        safe_key = grain_key.replace("/", "_").replace("\\", "_").replace("..", "_")
        return self._base_path / grain_type / f"{safe_key}.json"
