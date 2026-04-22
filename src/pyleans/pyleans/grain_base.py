"""Generic base class for grains, providing runtime-bound attributes."""

import contextvars
import logging

from pyleans.errors import GrainActivationError
from pyleans.identity import GrainId

_current_grain_id: contextvars.ContextVar[GrainId | None] = contextvars.ContextVar(
    "_current_grain_id", default=None
)


class Grain[TState]:
    """Base class for stateful grains.

    Provides runtime-bound attributes that the grain runtime sets during
    activation. Stub methods raise if called before activation.

    Only one type parameter (state) since grain keys are always strings.

    Usage::

        @grain(storage="default")
        class CounterGrain(Grain[CounterState]):
            def __init__(self):
                # self.identity is available here
                print(f"Constructing grain {self.identity.key}")

            async def get_value(self) -> int:
                self.logger.debug("get_value called")
                return self.state.value
    """

    state: TState

    @property
    def identity(self) -> GrainId:
        """The grain's unique identity (type + key).

        Available during ``__init__`` and throughout the grain's lifetime.
        """
        if "_identity" in self.__dict__:
            result: GrainId = self.__dict__["_identity"]
            return result
        gid = _current_grain_id.get()
        if gid is not None:
            return gid
        raise GrainActivationError("identity not available -- grain not activated")

    @identity.setter
    def identity(self, value: GrainId) -> None:
        self.__dict__["_identity"] = value

    @property
    def logger(self) -> logging.Logger:
        """Per-grain-type logger (``pyleans.grain.<GrainType>``)."""
        return logging.getLogger(f"pyleans.grain.{type(self).__name__}")

    async def read_state(self) -> None:
        """Reload current state from the storage provider."""
        raise GrainActivationError("read_state not bound -- grain not activated")

    async def write_state(self) -> None:
        """Persist the current state via the storage provider."""
        raise GrainActivationError("write_state not bound -- grain not activated")

    async def clear_state(self) -> None:
        """Clear persisted state and reset to defaults."""
        raise GrainActivationError("clear_state not bound -- grain not activated")

    def deactivate_on_idle(self) -> None:
        """Request deactivation after the current turn completes."""
        raise GrainActivationError("deactivate_on_idle not bound -- grain not activated")
