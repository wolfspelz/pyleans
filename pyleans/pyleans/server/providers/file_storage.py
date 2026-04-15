"""File-based storage provider — one JSON file per grain."""

import re
import uuid
from pathlib import Path
from typing import Any

import orjson

from pyleans.errors import StorageError, StorageInconsistencyError
from pyleans.providers.storage import StorageProvider

_SAFE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _sanitize_path_component(value: str) -> str:
    """Sanitize a string for use as a filesystem path component.

    Replaces any character that isn't alphanumeric, hyphen, or underscore.
    Rejects empty strings after sanitization.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "_", value)
    if not sanitized:
        raise StorageError(f"Path component is empty after sanitization: {value!r}")
    return sanitized


class FileStorageProvider(StorageProvider):
    """Stores grain state as individual JSON files.

    Directory layout: {base_path}/{grain_type}/{safe_key}.json

    Each file contains: {"etag": "uuid-string", "state": {...}}
    """

    def __init__(self, base_path: str = "./pyleans-data/storage") -> None:
        self._base_path = Path(base_path).resolve()

    async def read(
        self, grain_type: str, grain_key: str
    ) -> tuple[dict[str, Any], str | None]:
        path = self._grain_path(grain_type, grain_key)
        if not path.exists():
            return {}, None
        try:
            data = orjson.loads(path.read_bytes())
            return data["state"], data["etag"]
        except (orjson.JSONDecodeError, KeyError, OSError) as e:
            raise StorageError(
                f"Failed to read state for {grain_type}/{grain_key}: {e}"
            ) from e

    async def write(
        self,
        grain_type: str,
        grain_key: str,
        state: dict[str, Any],
        expected_etag: str | None,
    ) -> str:
        path = self._grain_path(grain_type, grain_key)
        self._check_etag(path, expected_etag)

        new_etag = str(uuid.uuid4())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(orjson.dumps({"etag": new_etag, "state": state}))
        except OSError as e:
            raise StorageError(
                f"Failed to write state for {grain_type}/{grain_key}: {e}"
            ) from e
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
        self._check_etag(path, expected_etag)
        try:
            path.unlink()
        except OSError as e:
            raise StorageError(
                f"Failed to clear state for {grain_type}/{grain_key}: {e}"
            ) from e

    def _grain_path(self, grain_type: str, grain_key: str) -> Path:
        """Build a safe filesystem path for a grain's state file.

        Sanitizes both grain_type and grain_key, then verifies the resolved
        path is within the base directory to prevent path traversal.
        """
        safe_type = _sanitize_path_component(grain_type)
        safe_key = _sanitize_path_component(grain_key)
        path = (self._base_path / safe_type / f"{safe_key}.json").resolve()
        if not str(path).startswith(str(self._base_path)):
            raise StorageError(
                f"Path traversal detected: {grain_type}/{grain_key}"
            )
        return path

    @staticmethod
    def _check_etag(path: Path, expected_etag: str | None) -> None:
        """Verify etag matches if file exists and etag is provided."""
        if expected_etag is None or not path.exists():
            return
        try:
            current = orjson.loads(path.read_bytes())
            actual_etag = current["etag"]
        except (orjson.JSONDecodeError, KeyError, OSError) as e:
            raise StorageError(f"Failed to read etag: {e}") from e
        if actual_etag != expected_etag:
            raise StorageInconsistencyError(expected_etag, actual_etag)
