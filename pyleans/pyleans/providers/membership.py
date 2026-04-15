"""Membership provider ABC — port for silo discovery and health."""

from abc import ABC, abstractmethod

from pyleans.identity import SiloInfo, SiloStatus


class MembershipProvider(ABC):
    """Pluggable membership interface for silo registration and discovery."""

    @abstractmethod
    async def register_silo(self, silo: SiloInfo) -> None:
        """Register a silo as joining/active."""
        ...

    @abstractmethod
    async def unregister_silo(self, silo_id: str) -> None:
        """Remove a silo from the membership table."""
        ...

    @abstractmethod
    async def get_active_silos(self) -> list[SiloInfo]:
        """Return all silos with status Active."""
        ...

    @abstractmethod
    async def heartbeat(self, silo_id: str) -> None:
        """Update the heartbeat timestamp for a silo."""
        ...

    @abstractmethod
    async def update_status(self, silo_id: str, status: SiloStatus) -> None:
        """Update a silo's status."""
        ...
