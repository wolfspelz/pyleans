"""Tests for pyleans.providers — provider ABCs."""

import pytest
from pyleans.providers import (
    MembershipProvider,
    StorageProvider,
    StreamProvider,
    StreamSubscription,
)


class TestStorageProviderABC:
    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            StorageProvider()  # type: ignore[abstract]

    def test_has_read_method(self) -> None:
        assert hasattr(StorageProvider, "read")

    def test_has_write_method(self) -> None:
        assert hasattr(StorageProvider, "write")

    def test_has_clear_method(self) -> None:
        assert hasattr(StorageProvider, "clear")

    def test_concrete_subclass(self) -> None:
        class FakeStorage(StorageProvider):
            async def read(self, grain_type: str, grain_key: str) -> tuple[dict, str | None]:
                return {}, None

            async def write(
                self,
                grain_type: str,
                grain_key: str,
                state: dict,
                expected_etag: str | None,
            ) -> str:
                return "etag1"

            async def clear(
                self,
                grain_type: str,
                grain_key: str,
                expected_etag: str | None,
            ) -> None:
                pass

        storage = FakeStorage()
        assert isinstance(storage, StorageProvider)


class TestMembershipProviderABC:
    def test_cannot_instantiate(self) -> None:
        # Act / Assert
        with pytest.raises(TypeError):
            MembershipProvider()  # type: ignore[abstract]

    def test_has_occ_primitives(self) -> None:
        # Assert
        for method in [
            "read_all",
            "read_silo",
            "try_update_silo",
            "try_add_suspicion",
            "try_delete_silo",
        ]:
            assert hasattr(MembershipProvider, method)

    def test_concrete_subclass_instantiates(self) -> None:
        # Arrange
        from pyleans.identity import SiloInfo, SuspicionVote
        from pyleans.providers.membership import MembershipSnapshot

        class FakeMembership(MembershipProvider):
            async def read_all(self) -> MembershipSnapshot:
                return MembershipSnapshot(version=0)

            async def try_update_silo(self, silo: SiloInfo) -> SiloInfo:
                return silo

            async def try_add_suspicion(
                self, silo_id: str, vote: SuspicionVote, expected_etag: str
            ) -> SiloInfo:
                raise NotImplementedError

            async def try_delete_silo(self, silo: SiloInfo) -> None:
                return None

        # Act
        membership = FakeMembership()

        # Assert
        assert isinstance(membership, MembershipProvider)


class TestStreamProviderABC:
    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            StreamProvider()  # type: ignore[abstract]

    def test_has_all_methods(self) -> None:
        for method in ["publish", "subscribe", "unsubscribe"]:
            assert hasattr(StreamProvider, method)

    def test_concrete_subclass(self) -> None:
        from collections.abc import Awaitable, Callable
        from typing import Any

        class FakeStream(StreamProvider):
            async def publish(self, stream_namespace: str, stream_key: str, event: Any) -> None:
                pass

            async def subscribe(
                self,
                stream_namespace: str,
                stream_key: str,
                callback: Callable[[Any], Awaitable[None]],
            ) -> StreamSubscription:
                return StreamSubscription(
                    id="sub-1",
                    stream_namespace=stream_namespace,
                    stream_key=stream_key,
                )

            async def unsubscribe(self, subscription: StreamSubscription) -> None:
                pass

        stream = FakeStream()
        assert isinstance(stream, StreamProvider)


class TestStreamSubscription:
    def test_creation(self) -> None:
        sub = StreamSubscription(id="sub-1", stream_namespace="chat", stream_key="room-1")
        assert sub.id == "sub-1"
        assert sub.stream_namespace == "chat"
        assert sub.stream_key == "room-1"

    def test_equality(self) -> None:
        a = StreamSubscription(id="sub-1", stream_namespace="chat", stream_key="room-1")
        b = StreamSubscription(id="sub-1", stream_namespace="chat", stream_key="room-1")
        assert a == b

    def test_inequality(self) -> None:
        a = StreamSubscription(id="sub-1", stream_namespace="chat", stream_key="room-1")
        b = StreamSubscription(id="sub-2", stream_namespace="chat", stream_key="room-1")
        assert a != b


class TestProviderImports:
    def test_importable_from_providers(self) -> None:
        from pyleans.providers import (  # noqa: F401
            MembershipProvider,
            StorageProvider,
            StreamProvider,
            StreamSubscription,
        )
