"""Storage provider ABC — port for grain state persistence."""

from abc import ABC, abstractmethod
from typing import Any


class StorageProvider(ABC):
    """Pluggable storage interface for grain state."""

    @abstractmethod
    async def read(self, grain_type: str, grain_key: str) -> tuple[dict[str, Any], str | None]:
        """Read grain state.

        Returns (state_dict, etag). Returns ({}, None) if not found.
        """
        ...

    @abstractmethod
    async def write(
        self,
        grain_type: str,
        grain_key: str,
        state: dict[str, Any],
        expected_etag: str | None,
    ) -> str:
        """Write grain state.

        Returns new etag. Raises StorageInconsistencyError on etag mismatch.
        """
        ...

    @abstractmethod
    async def clear(
        self,
        grain_type: str,
        grain_key: str,
        expected_etag: str | None,
    ) -> None:
        """Delete grain state."""
        ...
