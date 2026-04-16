"""Grain decorator and registry for virtual actors."""

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from pyleans.errors import GrainNotFoundError

_grain_registry: dict[str, type] = {}

LIFECYCLE_METHODS = frozenset({"on_activate", "on_deactivate"})


def _set_grain_metadata(cls: type, name: str, value: Any) -> None:
    """Set a metadata attribute on a grain class (used by the decorator)."""
    type.__setattr__(cls, name, value)


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
        _set_grain_metadata(cls, "_grain_type", cls.__name__)
        _set_grain_metadata(cls, "_state_type", state_type)
        _set_grain_metadata(cls, "_storage_name", storage)

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


def get_grain_type_name(grain_class: type) -> str:
    """Return the grain type name for a decorated grain class."""
    name: str = getattr(grain_class, "_grain_type", grain_class.__name__)
    return name


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
