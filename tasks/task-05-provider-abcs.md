# Task 05: Provider Abstract Base Classes

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-core-types.md](task-02-core-types.md)
- [task-03-errors.md](task-03-errors.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Decision 5 (Provider Interfaces)
- [orleans-persistence.md](../docs/orleans-persistence.md) -- IGrainStorage interface
- [orleans-cluster.md](../docs/orleans-cluster.md) -- Membership providers
- [orleans-streaming.md](../docs/orleans-streaming.md) -- Stream providers
- [hexagonal-architecture.md](../docs/hexagonal-architecture.md) -- Ports & Adapters pattern

## Description

Define the three provider ABCs. These are the pluggable extension points of pyleans,
following Orleans' provider model and hexagonal architecture principles.

### Files to create
- `src/pyleans/providers/storage.py`
- `src/pyleans/providers/membership.py`
- `src/pyleans/providers/streaming.py`
- `src/pyleans/providers/__init__.py` (re-exports)

### StorageProvider

```python
class StorageProvider(ABC):
    @abstractmethod
    async def read(self, grain_type: str, grain_key: str) -> tuple[dict, str | None]:
        """Read grain state. Returns (state_dict, etag). ({}, None) if not found."""
        ...

    @abstractmethod
    async def write(self, grain_type: str, grain_key: str,
                    state: dict, expected_etag: str | None) -> str:
        """Write grain state. Returns new etag. Raises StorageInconsistencyError on mismatch."""
        ...

    @abstractmethod
    async def clear(self, grain_type: str, grain_key: str,
                    expected_etag: str | None) -> None:
        """Delete grain state."""
        ...
```

### MembershipProvider

```python
class MembershipProvider(ABC):
    @abstractmethod
    async def register_silo(self, silo: SiloInfo) -> None:
        """Register a silo as joining/active."""
        ...

    @abstractmethod
    async def unregister_silo(self, silo_id: str) -> None:
        """Remove a silo from the membership table."""
        ...

    @abstractmethod
    async def get_active_silos(self) -> list[SiloInfo]:
        """Return all silos with status Active."""
        ...

    @abstractmethod
    async def heartbeat(self, silo_id: str) -> None:
        """Update the heartbeat timestamp for a silo."""
        ...

    @abstractmethod
    async def update_status(self, silo_id: str, status: SiloStatus) -> None:
        """Update a silo's status."""
        ...
```

### StreamProvider

```python
class StreamProvider(ABC):
    @abstractmethod
    async def publish(self, stream_namespace: str, stream_key: str,
                      event: Any) -> None:
        """Publish an event to a stream."""
        ...

    @abstractmethod
    async def subscribe(self, stream_namespace: str, stream_key: str,
                        callback: Callable[[Any], Awaitable[None]]) -> StreamSubscription:
        """Subscribe to a stream. Returns a handle for unsubscribing."""
        ...

    @abstractmethod
    async def unsubscribe(self, subscription: StreamSubscription) -> None:
        """Remove a subscription."""
        ...

@dataclass
class StreamSubscription:
    id: str
    stream_namespace: str
    stream_key: str
```

### Acceptance criteria

- [ ] All three ABCs defined with `@abstractmethod` on all methods
- [ ] Importable from `pyleans.providers`
- [ ] Type hints on all parameters and return types
- [ ] `StreamSubscription` dataclass defined

## Summary of implementation
_To be filled when task is complete._