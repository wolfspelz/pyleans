"""Counter grain — demonstrates stateful virtual actors with pyleans."""

from dataclasses import dataclass
from typing import Any

from dependency_injector.wiring import Provide, inject
from pyleans.server.container import PyleansContainer
from pyleans.server.silo_management import SiloManagement

from pyleans import grain


@dataclass
class CounterState:
    """Persistent state for the counter grain."""

    value: int = 0


@grain(state_type=CounterState, storage="default")
class CounterGrain:
    """A simple counter that persists its value across activations.

    Each counter is identified by a unique key and maintains independent state.
    """

    @inject
    def __init__(
        self,
        silo_mgmt: SiloManagement = Provide[PyleansContainer.silo_management],  # type: ignore[assignment]
    ) -> None:
        self._silo_mgmt = silo_mgmt

    async def get_value(self) -> int:
        """Return the current counter value."""
        return self.state.value  # type: ignore[attr-defined]

    async def increment(self) -> int:
        """Increment the counter by 1 and persist. Returns the new value."""
        self.state.value += 1  # type: ignore[attr-defined]
        await self.save_state()  # type: ignore[attr-defined]
        return self.state.value  # type: ignore[attr-defined]

    async def set_value(self, value: int) -> None:
        """Set the counter to a specific value and persist."""
        self.state.value = value  # type: ignore[attr-defined]
        await self.save_state()  # type: ignore[attr-defined]

    async def reset(self) -> None:
        """Reset the counter to zero and persist."""
        self.state.value = 0  # type: ignore[attr-defined]
        await self.save_state()  # type: ignore[attr-defined]

    async def get_silo_info(self) -> dict[str, Any]:
        """Return metadata about the silo hosting this grain."""
        return self._silo_mgmt.get_info()
