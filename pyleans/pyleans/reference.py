"""Grain reference (proxy) and factory for virtual actor calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyleans.identity import GrainId

if TYPE_CHECKING:
    from pyleans.server.runtime import GrainRuntime


class GrainRef:
    """A proxy for a grain that forwards method calls to the runtime.

    Creating a GrainRef does NOT activate the grain — activation happens
    on the first method call. GrainRef is lightweight and can be stored
    or passed around freely.
    """

    def __init__(self, grain_id: GrainId, runtime: GrainRuntime) -> None:
        self._grain_id = grain_id
        self._runtime = runtime

    @property
    def identity(self) -> GrainId:
        """The grain identity this reference points to."""
        return self._grain_id

    def __getattr__(self, name: str) -> Any:
        """Return an async callable that dispatches to the grain runtime."""
        if name.startswith("_"):
            raise AttributeError(name)

        async def _proxy_call(*args: Any, **kwargs: Any) -> Any:
            return await self._runtime.invoke(self._grain_id, name, list(args), kwargs)

        return _proxy_call

    def __repr__(self) -> str:
        return f"GrainRef({self._grain_id})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GrainRef):
            return NotImplemented
        return self._grain_id == other._grain_id

    def __hash__(self) -> int:
        return hash(self._grain_id)


class GrainFactory:
    """Creates grain references. Injected into grains via DI."""

    def __init__(self, runtime: GrainRuntime) -> None:
        self._runtime = runtime

    def get_grain(self, grain_class: type, key: str) -> GrainRef:
        """Get a reference to a grain by class and key.

        Does NOT activate the grain — activation happens on first call.
        """
        grain_type: str = grain_class._grain_type  # type: ignore[attr-defined]
        grain_id = GrainId(grain_type=grain_type, key=key)
        return GrainRef(grain_id=grain_id, runtime=self._runtime)
