"""Counter grain — demonstrates stateful virtual actors with pyleans."""

from dataclasses import dataclass
from typing import Any

from pyleans.server.silo_management import SiloManagement

from pyleans import Grain, grain


@dataclass
class CounterState:
    """Persistent state for the counter grain."""

    value: int = 0


@grain(storage="default")
class CounterGrain(Grain[CounterState]):
    """A simple counter that persists its value across activations.

    Each counter is identified by a unique key and maintains independent state.
    """

    def __init__(self, silo_mgmt: SiloManagement) -> None:
        self._silo_mgmt = silo_mgmt
        self.logger.info("Counter %s created", self.identity.key)

    async def get_value(self) -> int:
        """Return the current counter value."""
        self.logger.info("Counter %s get_value -> %d", self.identity.key, self.state.value)
        return self.state.value

    async def increment(self) -> int:
        """Increment the counter by 1 and persist. Returns the new value."""
        self.state.value += 1
        await self.write_state()
        self.logger.info("Counter %s incremented to %d", self.identity.key, self.state.value)
        return self.state.value

    async def set_value(self, value: int) -> None:
        """Set the counter to a specific value and persist."""
        self.state.value = value
        await self.write_state()
        self.logger.info("Counter %s set to %d", self.identity.key, value)

    async def reset(self) -> None:
        """Reset the counter to zero and persist."""
        self.state.value = 0
        await self.write_state()
        self.logger.info("Counter %s reset", self.identity.key)

    async def reload(self) -> int:
        """Reload state from storage and return the current value."""
        await self.read_state()
        self.logger.info("Counter %s reloaded -> %d", self.identity.key, self.state.value)
        return self.state.value

    async def get_silo_info(self) -> dict[str, Any]:
        """Return metadata about the silo hosting this grain."""
        self.logger.info("Counter %s get_silo_info", self.identity.key)
        return self._silo_mgmt.get_info()
