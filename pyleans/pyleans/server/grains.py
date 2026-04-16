"""Framework-provided grains for silo management."""

from pyleans.server.string_cache_grain import StringCacheGrain

__all__ = ["StringCacheGrain", "system_grains"]


def system_grains() -> list[type]:
    """Return the list of framework-provided grains.

    Use this in silo configuration to include pyleans system grains::

        from pyleans.server.grains import system_grains

        silo = Silo(
            grains=[CounterGrain, *system_grains()],
            ...
        )

    Currently includes:

    - ``StringCacheGrain`` — simple string key-value store with persistence
    """
    return [StringCacheGrain]
