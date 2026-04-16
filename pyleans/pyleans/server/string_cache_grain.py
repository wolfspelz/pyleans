"""StringCacheGrain — framework-provided string key-value store grain."""

from dataclasses import dataclass

from pyleans.grain import grain


@dataclass
class StringCacheState:
    """Persistent state for the string cache grain."""

    value: str = ""


@grain(state_type=StringCacheState, storage="default")
class StringCacheGrain:
    """Simple string key-value store grain.

    Each grain instance (identified by key) holds one string value.
    State is persisted — survives silo restarts.

    Usage::

        cache = client.get_grain(StringCacheGrain, "my-key")
        await cache.set("hello world")
        value = await cache.get()       # "hello world"
        await cache.delete()            # clears persisted state
        await cache.deactivate()        # removes from memory
    """

    async def set(self, value: str) -> None:
        """Set the cached value and persist."""
        self.state.value = value  # type: ignore[attr-defined]
        await self.save_state()  # type: ignore[attr-defined]

    async def get(self) -> str:
        """Return the cached value (empty string if never set)."""
        return self.state.value  # type: ignore[attr-defined, no-any-return]

    async def delete(self) -> None:
        """Clear the persisted state (resets value to empty string)."""
        await self.clear_state()  # type: ignore[attr-defined]

    async def deactivate(self) -> None:
        """Remove this grain from memory.

        The next call to this grain will re-activate it from persistence.
        """
        self.request_deactivation()  # type: ignore[attr-defined]
