# Task 13: In-Memory Stream Provider

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-05-provider-abcs.md](task-05-provider-abcs.md)
- [task-07-grain-runtime.md](task-07-grain-runtime.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Phase 1, Decision 5
- [orleans-streaming.md](../docs/orleans-streaming.md) -- Stream concepts, PubSub

## Description

Implement an in-memory stream provider for single-silo use. Streams are
pub/sub channels identified by (namespace, key). Subscribers receive events
through their grain inbox (turn-based).

### Files to create
- `src/pyleans/server/providers/memory_stream.py`

### Design

```python
class InMemoryStreamProvider(StreamProvider):
    """
    In-memory pub/sub streams. Events are delivered to subscribers
    within the same silo process. Not durable -- subscriptions and
    events are lost on silo restart.
    """

    def __init__(self):
        # stream_key -> list of subscriptions
        self._subscriptions: dict[tuple[str, str], list[_Subscription]] = {}

    async def publish(self, stream_namespace, stream_key, event):
        key = (stream_namespace, stream_key)
        for sub in self._subscriptions.get(key, []):
            # Deliver through grain inbox if subscriber is a grain
            # Otherwise call callback directly
            asyncio.create_task(sub.callback(event))

    async def subscribe(self, stream_namespace, stream_key, callback):
        key = (stream_namespace, stream_key)
        sub_id = str(uuid.uuid4())
        subscription = _Subscription(id=sub_id, callback=callback)
        self._subscriptions.setdefault(key, []).append(subscription)
        return StreamSubscription(
            id=sub_id,
            stream_namespace=stream_namespace,
            stream_key=stream_key,
        )

    async def unsubscribe(self, subscription):
        key = (subscription.stream_namespace, subscription.stream_key)
        subs = self._subscriptions.get(key, [])
        self._subscriptions[key] = [s for s in subs if s.id != subscription.id]
```

### StreamManager

Wraps the stream provider and provides a grain-friendly API:

```python
class StreamManager:
    """Injected into grains via DI. Provides stream access."""

    def __init__(self, provider: StreamProvider):
        self._provider = provider

    def get_stream(self, namespace: str, key: str) -> StreamRef:
        return StreamRef(namespace, key, self._provider)

class StreamRef:
    """Reference to a specific stream."""

    async def publish(self, event: Any) -> None: ...
    async def subscribe(self, callback) -> StreamSubscription: ...
```

### Grain usage

```python
@grain
class RoomGrain:
    @inject
    def __init__(self, streams: StreamManager = Provide[...]):
        self.streams = streams

    async def on_activate(self):
        self.chat_stream = self.streams.get_stream("chat", self.identity.key)

    async def send_message(self, user: str, text: str):
        await self.chat_stream.publish({"user": user, "text": text})
```

### Acceptance criteria

- [x] Publish delivers to all subscribers
- [x] Subscribe returns a StreamSubscription handle
- [x] Unsubscribe stops delivery
- [x] Multiple subscribers on same stream
- [x] Multiple independent streams
- [x] Publishing to stream with no subscribers is a no-op
- [x] Unit tests for pub/sub flow

## Findings of code review
- Removed unused imports (`asyncio`, `field`) — fixed.
- All classes have type annotations, follow SOLID/KISS principles.
- No dead code, unused imports, or magic constants.

## Findings of security review
- No vulnerabilities found. Minimal attack surface (no I/O, no deserialization, internal API only).
- No unbounded resource consumption beyond what the design requires.

## Summary of implementation

### Files created
- `pyleans/pyleans/server/providers/memory_stream.py` — `InMemoryStreamProvider`, `StreamRef`, `StreamManager`
- `pyleans/test/test_memory_stream.py` — 24 unit tests

### Key implementation decisions
- **Synchronous delivery**: publish awaits each callback sequentially, preserving event ordering and surfacing errors immediately via `ExceptionGroup`. Safer than fire-and-forget `create_task`.
- **Cleanup on empty**: when all subscriptions for a stream key are removed, the key is deleted from the internal dict to prevent memory leaks.
- **Three-class design**: `InMemoryStreamProvider` (implements ABC), `StreamRef` (bound to namespace+key), `StreamManager` (DI-friendly factory for StreamRefs).

### Deviations from task design
- Callbacks are awaited sequentially instead of using `asyncio.create_task` — better error handling and ordering guarantees.
- Errors from callbacks are collected into an `ExceptionGroup` instead of being silently ignored.

### Test coverage (24 tests)
- Publish/subscribe happy path, multiple subscribers, independent streams, namespace isolation
- Unsubscribe stops delivery, partial unsubscribe, double unsubscribe safety, cleanup
- Error handling: bad callbacks raise ExceptionGroup, good callbacks still called
- StreamRef and StreamManager wrappers, cross-ref publish/subscribe