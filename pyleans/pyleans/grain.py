"""Grain decorator and registry for virtual actors."""

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from pyleans.errors import GrainNotFoundError
from pyleans.identity import GrainId

_grain_registry: dict[str, type] = {}

LIFECYCLE_METHODS = frozenset({"on_activate", "on_deactivate"})


def grain(
    cls: type | None = None,
    *,
    state_type: type | None = None,
    storage: str = "default",
) -> Any:
    """Mark a class as a grain (virtual actor).

    Can be used with or without arguments:
        @grain
        class MyGrain: ...

        @grain(state_type=MyState, storage="default")
        class MyGrain: ...
    """
    def decorator(cls: type) -> type:
        cls._grain_type = cls.__name__  # type: ignore[attr-defined]
        cls._state_type = state_type  # type: ignore[attr-defined]
        cls._storage_name = storage  # type: ignore[attr-defined]

        _grain_registry[cls.__name__] = cls
        return cls

    if cls is not None:
        return decorator(cls)
    return decorator


def get_grain_class(grain_type: str) -> type:
    """Look up a registered grain class by type name."""
    try:
        return _grain_registry[grain_type]
    except KeyError:
        raise GrainNotFoundError(f"Grain type {grain_type!r} not registered") from None


def get_grain_methods(grain_class: type) -> dict[str, Callable[..., Any]]:
    """Return public async methods that form the grain's callable interface.

    Excludes private methods (starting with _) and lifecycle hooks.
    """
    methods: dict[str, Callable[..., Any]] = {}
    for name, method in inspect.getmembers(grain_class, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        if name in LIFECYCLE_METHODS:
            continue
        if asyncio.iscoroutinefunction(method):
            methods[name] = method
    return methods
