---
status: accepted
date: 2026-04-18
tags: [providers, ports, hexagonal]
---

# Provider Interfaces as Hexagonal Ports (ABCs)

## Context and Problem Statement

Storage, membership, and streaming each have many possible backends (local file, Redis, S3, etcd, …). Per hexagonal architecture, the grain runtime must depend on abstractions, not specific backends. We need minimal ABCs that cover PoC needs without over-engineering.

## Decision

Minimal ABCs with the minimum required methods. Each provider type has one ABC (port) and one or more concrete adapters. All external dependencies are accessed through these interfaces.

**Storage Provider**:

```python
class StorageProvider(ABC):
    @abstractmethod
    async def read(self, grain_type: str, grain_key: str) -> tuple[Any, str | None]:
        """Returns (state_dict, etag). Returns ({}, None) if not found."""
        ...

    @abstractmethod
    async def write(self, grain_type: str, grain_key: str,
                    state: Any, expected_etag: str | None) -> str:
        """Writes state, returns new etag. Raises on etag mismatch."""
        ...

    @abstractmethod
    async def clear(self, grain_type: str, grain_key: str,
                    expected_etag: str | None) -> None:
        """Deletes grain state."""
        ...
```

**Membership Provider**:

```python
class MembershipProvider(ABC):
    @abstractmethod
    async def register_silo(self, silo: SiloInfo) -> None: ...

    @abstractmethod
    async def unregister_silo(self, silo_id: str) -> None: ...

    @abstractmethod
    async def get_active_silos(self) -> list[SiloInfo]: ...

    @abstractmethod
    async def heartbeat(self, silo_id: str) -> None: ...
```

**Stream Provider**:

```python
class StreamProvider(ABC):
    @abstractmethod
    async def publish(self, stream_ns: str, stream_key: str,
                      event: Any) -> None: ...

    @abstractmethod
    async def subscribe(self, stream_ns: str, stream_key: str,
                        callback: Callable) -> StreamSubscription: ...

    @abstractmethod
    async def unsubscribe(self, subscription: StreamSubscription) -> None: ...
```

**Default adapters (PoC)**:

- Storage: `FileStorageProvider` (one JSON file per grain).
- Membership: `YamlMembershipProvider` (shared YAML file) or `MarkdownTableMembershipProvider` (human-readable Markdown table for dev inspection).
- Streaming: `InMemoryStreamProvider` (asyncio queues, single-silo only).

## Consequences

- Core runtime has zero dependencies on concrete backends.
- Adding Redis/S3/etcd is a new adapter; no runtime changes.
- Etag-based optimistic concurrency is baked into the storage contract — all storage adapters must support it.

## Related

- [adr-serialization](adr-serialization.md) — providers use the `Serializer` port.
- [adr-dev-mode](adr-dev-mode.md) — in-memory defaults.
