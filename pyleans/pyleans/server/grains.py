"""Framework-provided grains for silo management."""

__all__ = ["system_grains"]


def system_grains() -> list[type]:
    """Return the list of framework-provided grains.

    Reserved for future use. Currently returns an empty list.
    Use this in silo configuration to future-proof your grain list::

        from pyleans.server.grains import system_grains

        silo = Silo(
            grains=[CounterGrain, *system_grains()],
            ...
        )
    """
    return []
