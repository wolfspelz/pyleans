"""In-memory stream provider for single-silo pub/sub streaming."""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pyleans.providers.streaming import StreamProvider, StreamSubscription

logger = logging.getLogger(__name__)


@dataclass
class _Subscription:
    """Internal subscription record."""

    id: str
    callback: Callable[[Any], Awaitable[None]]


StreamKey = tuple[str, str]


class InMemoryStreamProvider(StreamProvider):
    """In-memory pub/sub streams for single-silo use.

    Events are delivered to subscribers within the same silo process.
    Not durable — subscriptions and events are lost on silo restart.
    """

    def __init__(self) -> None:
        self._subscriptions: dict[StreamKey, list[_Subscription]] = {}

    async def publish(self, stream_namespace: str, stream_key: str, event: Any) -> None:
        """Publish an event, delivering to all current subscribers."""
        key: StreamKey = (stream_namespace, stream_key)
        subscribers = self._subscriptions.get(key, [])
        logger.debug(
            "Publishing to %s/%s (%d subscribers)", stream_namespace, stream_key, len(subscribers)
        )
        errors: list[Exception] = []
        for sub in subscribers:
            try:
                await sub.callback(event)
            except Exception as e:
                logger.warning(
                    "Subscriber %s failed on %s/%s: %s", sub.id, stream_namespace, stream_key, e
                )
                errors.append(e)
        if errors:
            raise ExceptionGroup("stream delivery failures", errors)

    async def subscribe(
        self,
        stream_namespace: str,
        stream_key: str,
        callback: Callable[[Any], Awaitable[None]],
    ) -> StreamSubscription:
        """Subscribe to a stream. Returns a handle for unsubscribing."""
        key: StreamKey = (stream_namespace, stream_key)
        sub_id = str(uuid.uuid4())
        subscription = _Subscription(id=sub_id, callback=callback)
        self._subscriptions.setdefault(key, []).append(subscription)
        logger.debug("Subscriber added to %s/%s (id=%s)", stream_namespace, stream_key, sub_id)
        return StreamSubscription(
            id=sub_id,
            stream_namespace=stream_namespace,
            stream_key=stream_key,
        )

    async def unsubscribe(self, subscription: StreamSubscription) -> None:
        """Remove a subscription by its handle."""
        key: StreamKey = (subscription.stream_namespace, subscription.stream_key)
        subs = self._subscriptions.get(key, [])
        self._subscriptions[key] = [s for s in subs if s.id != subscription.id]
        logger.debug(
            "Subscriber removed from %s/%s (id=%s)",
            subscription.stream_namespace,
            subscription.stream_key,
            subscription.id,
        )
        if not self._subscriptions[key]:
            del self._subscriptions[key]


class StreamRef:
    """Reference to a specific stream, providing publish/subscribe operations."""

    def __init__(self, namespace: str, key: str, provider: StreamProvider) -> None:
        self._namespace = namespace
        self._key = key
        self._provider = provider

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def key(self) -> str:
        return self._key

    async def publish(self, event: Any) -> None:
        """Publish an event to this stream."""
        await self._provider.publish(self._namespace, self._key, event)

    async def subscribe(self, callback: Callable[[Any], Awaitable[None]]) -> StreamSubscription:
        """Subscribe to this stream."""
        return await self._provider.subscribe(self._namespace, self._key, callback)

    async def unsubscribe(self, subscription: StreamSubscription) -> None:
        """Unsubscribe from this stream."""
        await self._provider.unsubscribe(subscription)


class StreamManager:
    """Grain-friendly API for stream access. Injected via DI."""

    def __init__(self, provider: StreamProvider) -> None:
        self._provider = provider

    def get_stream(self, namespace: str, key: str) -> StreamRef:
        """Get a reference to a named stream."""
        return StreamRef(namespace, key, self._provider)
