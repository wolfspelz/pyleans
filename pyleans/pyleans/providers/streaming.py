"""Stream provider ABC — port for pub/sub event streaming."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class StreamSubscription:
    """Handle returned by subscribe, used to unsubscribe."""

    id: str
    stream_namespace: str
    stream_key: str


class StreamProvider(ABC):
    """Pluggable stream interface for pub/sub event delivery."""

    @abstractmethod
    async def publish(
        self, stream_namespace: str, stream_key: str, event: Any
    ) -> None:
        """Publish an event to a stream."""
        ...

    @abstractmethod
    async def subscribe(
        self,
        stream_namespace: str,
        stream_key: str,
        callback: Callable[[Any], Awaitable[None]],
    ) -> StreamSubscription:
        """Subscribe to a stream. Returns a handle for unsubscribing."""
        ...

    @abstractmethod
    async def unsubscribe(self, subscription: StreamSubscription) -> None:
        """Remove a subscription."""
        ...
