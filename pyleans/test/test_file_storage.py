"""Tests for file storage provider."""

import tempfile

import pytest

from pyleans.errors import StorageInconsistencyError
from pyleans.server.providers.file_storage import FileStorageProvider


@pytest.fixture
def storage(tmp_path: object) -> FileStorageProvider:
    return FileStorageProvider(base_path=str(tmp_path))


class TestFileStorageRead:
    async def test_read_nonexistent_returns_empty(
        self, storage: FileStorageProvider
    ) -> None:
        state, etag = await storage.read("Counter", "c1")
        assert state == {}
        assert etag is None

    async def test_read_after_write(self, storage: FileStorageProvider) -> None:
        await storage.write("Counter", "c1", {"value": 42}, None)
        state, etag = await storage.read("Counter", "c1")
        assert state == {"value": 42}
        assert etag is not None


class TestFileStorageWrite:
    async def test_first_write_succeeds(self, storage: FileStorageProvider) -> None:
        etag = await storage.write("Counter", "c1", {"value": 1}, None)
        assert etag is not None

    async def test_write_creates_directory(self, storage: FileStorageProvider) -> None:
        await storage.write("NewType", "k1", {"x": 1}, None)
        state, _ = await storage.read("NewType", "k1")
        assert state == {"x": 1}

    async def test_write_with_correct_etag(
        self, storage: FileStorageProvider
    ) -> None:
        etag1 = await storage.write("Counter", "c1", {"value": 1}, None)
        etag2 = await storage.write("Counter", "c1", {"value": 2}, etag1)
        assert etag2 != etag1

        state, etag = await storage.read("Counter", "c1")
        assert state == {"value": 2}
        assert etag == etag2

    async def test_write_with_wrong_etag_raises(
        self, storage: FileStorageProvider
    ) -> None:
        await storage.write("Counter", "c1", {"value": 1}, None)
        with pytest.raises(StorageInconsistencyError):
            await storage.write("Counter", "c1", {"value": 2}, "wrong-etag")

    async def test_write_with_none_etag_overwrites(
        self, storage: FileStorageProvider
    ) -> None:
        await storage.write("Counter", "c1", {"value": 1}, None)
        etag = await storage.write("Counter", "c1", {"value": 2}, None)
        state, _ = await storage.read("Counter", "c1")
        assert state == {"value": 2}

    async def test_etags_are_unique(self, storage: FileStorageProvider) -> None:
        etag1 = await storage.write("Counter", "c1", {"value": 1}, None)
        etag2 = await storage.write("Counter", "c2", {"value": 2}, None)
        assert etag1 != etag2


class TestFileStorageClear:
    async def test_clear_removes_data(self, storage: FileStorageProvider) -> None:
        etag = await storage.write("Counter", "c1", {"value": 1}, None)
        await storage.clear("Counter", "c1", etag)
        state, read_etag = await storage.read("Counter", "c1")
        assert state == {}
        assert read_etag is None

    async def test_clear_nonexistent_is_noop(
        self, storage: FileStorageProvider
    ) -> None:
        await storage.clear("Counter", "nope", None)

    async def test_clear_with_wrong_etag_raises(
        self, storage: FileStorageProvider
    ) -> None:
        await storage.write("Counter", "c1", {"value": 1}, None)
        with pytest.raises(StorageInconsistencyError):
            await storage.clear("Counter", "c1", "wrong-etag")

    async def test_clear_with_none_etag(
        self, storage: FileStorageProvider
    ) -> None:
        await storage.write("Counter", "c1", {"value": 1}, None)
        await storage.clear("Counter", "c1", None)
        state, _ = await storage.read("Counter", "c1")
        assert state == {}


class TestFileStorageDirectoryStructure:
    async def test_grain_path_structure(self, storage: FileStorageProvider) -> None:
        await storage.write("Counter", "c1", {"value": 1}, None)
        path = storage._grain_path("Counter", "c1")
        assert path.exists()
        assert path.parent.name == "Counter"
        assert path.name == "c1.json"

    async def test_key_sanitization(self, storage: FileStorageProvider) -> None:
        await storage.write("Counter", "a/b\\c..d", {"value": 1}, None)
        path = storage._grain_path("Counter", "a/b\\c..d")
        assert "/" not in path.name
        assert "\\" not in path.name
        assert ".." not in path.name


class TestFileStorageRoundTrip:
    async def test_full_lifecycle(self, storage: FileStorageProvider) -> None:
        state, etag = await storage.read("Player", "p1")
        assert state == {}
        assert etag is None

        etag = await storage.write("Player", "p1", {"name": "Alice", "level": 5}, None)
        state, read_etag = await storage.read("Player", "p1")
        assert state == {"name": "Alice", "level": 5}
        assert read_etag == etag

        etag2 = await storage.write(
            "Player", "p1", {"name": "Alice", "level": 6}, etag
        )
        state, _ = await storage.read("Player", "p1")
        assert state["level"] == 6

        await storage.clear("Player", "p1", etag2)
        state, etag = await storage.read("Player", "p1")
        assert state == {}
