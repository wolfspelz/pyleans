"""Tests for the in-memory stream provider, StreamRef, and StreamManager."""

from typing import Any

import pytest
from pyleans.providers.streaming import StreamProvider, StreamSubscription
from pyleans.server.providers.memory_stream import (
    InMemoryStreamProvider,
    StreamManager,
    StreamRef,
)

# ---------------------------------------------------------------------------
# InMemoryStreamProvider
# ---------------------------------------------------------------------------


class TestInMemoryStreamProviderIsAStreamProvider:
    def test_is_subclass(self) -> None:
        assert issubclass(InMemoryStreamProvider, StreamProvider)

    def test_instance_check(self) -> None:
        provider = InMemoryStreamProvider()
        assert isinstance(provider, StreamProvider)


class TestPublishSubscribe:
    @pytest.fixture()
    def provider(self) -> InMemoryStreamProvider:
        return InMemoryStreamProvider()

    @pytest.mark.asyncio()
    async def test_publish_delivers_to_subscriber(self, provider: InMemoryStreamProvider) -> None:
        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        await provider.subscribe("ns", "key1", handler)
        await provider.publish("ns", "key1", {"msg": "hello"})

        assert received == [{"msg": "hello"}]

    @pytest.mark.asyncio()
    async def test_subscribe_returns_stream_subscription(
        self, provider: InMemoryStreamProvider
    ) -> None:
        async def handler(event: Any) -> None:
            pass

        sub = await provider.subscribe("chat", "room-1", handler)

        assert isinstance(sub, StreamSubscription)
        assert sub.stream_namespace == "chat"
        assert sub.stream_key == "room-1"
        assert len(sub.id) > 0

    @pytest.mark.asyncio()
    async def test_multiple_subscribers_all_receive(self, provider: InMemoryStreamProvider) -> None:
        received_a: list[Any] = []
        received_b: list[Any] = []

        async def handler_a(event: Any) -> None:
            received_a.append(event)

        async def handler_b(event: Any) -> None:
            received_b.append(event)

        await provider.subscribe("ns", "k", handler_a)
        await provider.subscribe("ns", "k", handler_b)
        await provider.publish("ns", "k", 42)

        assert received_a == [42]
        assert received_b == [42]

    @pytest.mark.asyncio()
    async def test_independent_streams_isolated(self, provider: InMemoryStreamProvider) -> None:
        received_1: list[Any] = []
        received_2: list[Any] = []

        async def handler_1(event: Any) -> None:
            received_1.append(event)

        async def handler_2(event: Any) -> None:
            received_2.append(event)

        await provider.subscribe("ns", "stream-1", handler_1)
        await provider.subscribe("ns", "stream-2", handler_2)

        await provider.publish("ns", "stream-1", "event-for-1")
        await provider.publish("ns", "stream-2", "event-for-2")

        assert received_1 == ["event-for-1"]
        assert received_2 == ["event-for-2"]

    @pytest.mark.asyncio()
    async def test_different_namespaces_isolated(self, provider: InMemoryStreamProvider) -> None:
        received_a: list[Any] = []
        received_b: list[Any] = []

        async def handler_a(event: Any) -> None:
            received_a.append(event)

        async def handler_b(event: Any) -> None:
            received_b.append(event)

        await provider.subscribe("ns-a", "key", handler_a)
        await provider.subscribe("ns-b", "key", handler_b)

        await provider.publish("ns-a", "key", "a-event")

        assert received_a == ["a-event"]
        assert received_b == []

    @pytest.mark.asyncio()
    async def test_publish_no_subscribers_is_noop(self, provider: InMemoryStreamProvider) -> None:
        # Should not raise
        await provider.publish("ns", "nobody", "event")

    @pytest.mark.asyncio()
    async def test_publish_multiple_events_ordered(self, provider: InMemoryStreamProvider) -> None:
        received: list[int] = []

        async def handler(event: Any) -> None:
            received.append(event)

        await provider.subscribe("ns", "k", handler)
        for i in range(5):
            await provider.publish("ns", "k", i)

        assert received == [0, 1, 2, 3, 4]


class TestUnsubscribe:
    @pytest.fixture()
    def provider(self) -> InMemoryStreamProvider:
        return InMemoryStreamProvider()

    @pytest.mark.asyncio()
    async def test_unsubscribe_stops_delivery(self, provider: InMemoryStreamProvider) -> None:
        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        sub = await provider.subscribe("ns", "k", handler)
        await provider.publish("ns", "k", "before")
        await provider.unsubscribe(sub)
        await provider.publish("ns", "k", "after")

        assert received == ["before"]

    @pytest.mark.asyncio()
    async def test_unsubscribe_one_of_many(self, provider: InMemoryStreamProvider) -> None:
        received_a: list[Any] = []
        received_b: list[Any] = []

        async def handler_a(event: Any) -> None:
            received_a.append(event)

        async def handler_b(event: Any) -> None:
            received_b.append(event)

        sub_a = await provider.subscribe("ns", "k", handler_a)
        await provider.subscribe("ns", "k", handler_b)

        await provider.unsubscribe(sub_a)
        await provider.publish("ns", "k", "after-unsub")

        assert received_a == []
        assert received_b == ["after-unsub"]

    @pytest.mark.asyncio()
    async def test_unsubscribe_nonexistent_is_safe(self, provider: InMemoryStreamProvider) -> None:
        fake_sub = StreamSubscription(id="does-not-exist", stream_namespace="ns", stream_key="k")
        # Should not raise
        await provider.unsubscribe(fake_sub)

    @pytest.mark.asyncio()
    async def test_double_unsubscribe_is_safe(self, provider: InMemoryStreamProvider) -> None:
        async def handler(event: Any) -> None:
            pass

        sub = await provider.subscribe("ns", "k", handler)
        await provider.unsubscribe(sub)
        # Second unsubscribe should not raise
        await provider.unsubscribe(sub)

    @pytest.mark.asyncio()
    async def test_unsubscribe_all_cleans_up_stream_key(
        self, provider: InMemoryStreamProvider
    ) -> None:
        async def handler(event: Any) -> None:
            pass

        sub = await provider.subscribe("ns", "k", handler)
        await provider.unsubscribe(sub)

        # Internal state should be cleaned up
        assert ("ns", "k") not in provider._subscriptions


class TestPublishErrorHandling:
    @pytest.mark.asyncio()
    async def test_callback_error_raises_exception_group(self) -> None:
        provider = InMemoryStreamProvider()

        async def bad_handler(event: Any) -> None:
            raise ValueError("oops")

        await provider.subscribe("ns", "k", bad_handler)

        with pytest.raises(ExceptionGroup) as exc_info:
            await provider.publish("ns", "k", "event")

        assert len(exc_info.value.exceptions) == 1
        assert isinstance(exc_info.value.exceptions[0], ValueError)

    @pytest.mark.asyncio()
    async def test_one_bad_callback_others_still_called(self) -> None:
        provider = InMemoryStreamProvider()
        received: list[Any] = []

        async def good_handler(event: Any) -> None:
            received.append(event)

        async def bad_handler(event: Any) -> None:
            raise RuntimeError("fail")

        # Subscribe good first, then bad, then good again
        await provider.subscribe("ns", "k", good_handler)
        await provider.subscribe("ns", "k", bad_handler)
        await provider.subscribe("ns", "k", good_handler)

        with pytest.raises(ExceptionGroup):
            await provider.publish("ns", "k", "data")

        # Both good handlers should have been called despite the bad one
        assert received == ["data", "data"]


# ---------------------------------------------------------------------------
# StreamRef
# ---------------------------------------------------------------------------


class TestStreamRef:
    @pytest.fixture()
    def provider(self) -> InMemoryStreamProvider:
        return InMemoryStreamProvider()

    def test_properties(self, provider: InMemoryStreamProvider) -> None:
        ref = StreamRef("chat", "room-1", provider)
        assert ref.namespace == "chat"
        assert ref.key == "room-1"

    @pytest.mark.asyncio()
    async def test_publish_through_ref(self, provider: InMemoryStreamProvider) -> None:
        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        await provider.subscribe("ns", "k", handler)
        ref = StreamRef("ns", "k", provider)
        await ref.publish("hello")

        assert received == ["hello"]

    @pytest.mark.asyncio()
    async def test_subscribe_through_ref(self, provider: InMemoryStreamProvider) -> None:
        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        ref = StreamRef("ns", "k", provider)
        sub = await ref.subscribe(handler)

        assert isinstance(sub, StreamSubscription)
        await provider.publish("ns", "k", "via-provider")

        assert received == ["via-provider"]

    @pytest.mark.asyncio()
    async def test_unsubscribe_through_ref(self, provider: InMemoryStreamProvider) -> None:
        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        ref = StreamRef("ns", "k", provider)
        sub = await ref.subscribe(handler)
        await ref.publish("before")
        await ref.unsubscribe(sub)
        await ref.publish("after")

        assert received == ["before"]


# ---------------------------------------------------------------------------
# StreamManager
# ---------------------------------------------------------------------------


class TestStreamManager:
    @pytest.fixture()
    def provider(self) -> InMemoryStreamProvider:
        return InMemoryStreamProvider()

    @pytest.fixture()
    def manager(self, provider: InMemoryStreamProvider) -> StreamManager:
        return StreamManager(provider)

    def test_get_stream_returns_stream_ref(self, manager: StreamManager) -> None:
        ref = manager.get_stream("chat", "room-1")
        assert isinstance(ref, StreamRef)
        assert ref.namespace == "chat"
        assert ref.key == "room-1"

    @pytest.mark.asyncio()
    async def test_end_to_end_through_manager(self, manager: StreamManager) -> None:
        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        stream = manager.get_stream("events", "user-123")
        sub = await stream.subscribe(handler)
        await stream.publish({"action": "login"})

        assert received == [{"action": "login"}]

        await stream.unsubscribe(sub)
        await stream.publish({"action": "logout"})

        assert received == [{"action": "login"}]

    def test_different_get_stream_calls_same_underlying(self, manager: StreamManager) -> None:
        ref_a = manager.get_stream("ns", "k")
        ref_b = manager.get_stream("ns", "k")
        # They share the same provider so publish on one is visible to subscriptions on the other
        assert ref_a._provider is ref_b._provider

    @pytest.mark.asyncio()
    async def test_cross_ref_pub_sub(self, manager: StreamManager) -> None:
        received: list[Any] = []

        async def handler(event: Any) -> None:
            received.append(event)

        ref_sub = manager.get_stream("ns", "k")
        ref_pub = manager.get_stream("ns", "k")

        await ref_sub.subscribe(handler)
        await ref_pub.publish("cross-ref")

        assert received == ["cross-ref"]
