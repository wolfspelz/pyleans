"""Generic base class for grains, providing runtime-bound attributes."""

from pyleans.errors import GrainActivationError
from pyleans.identity import GrainId


class Grain[TState]:
    """Base class for stateful grains.

    Provides runtime-bound attributes that the grain runtime sets during
    activation. Stub methods raise if called before activation.

    Only one type parameter (state) since grain keys are always strings.

    Usage::

        @grain(storage="default")
        class CounterGrain(Grain[CounterState]):
            async def get_value(self) -> int:
                return self.state.value
    """

    identity: GrainId
    state: TState

    async def write_state(self) -> None:
        """Persist the current state via the storage provider."""
        raise GrainActivationError("write_state not bound -- grain not activated")

    async def clear_state(self) -> None:
        """Clear persisted state and reset to defaults."""
        raise GrainActivationError("clear_state not bound -- grain not activated")

    def deactivate_on_idle(self) -> None:
        """Request deactivation after the current turn completes."""
        raise GrainActivationError("deactivate_on_idle not bound -- grain not activated")
