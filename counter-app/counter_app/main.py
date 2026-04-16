"""Standalone silo hosting CounterGrain."""

import asyncio

from pyleans.server import Silo
from pyleans.server.grains import system_grains
from pyleans.server.providers import FileStorageProvider, YamlMembershipProvider

from counter_app.grains import CounterGrain


async def main() -> None:
    silo = Silo(
        grains=[CounterGrain, *system_grains()],
        storage_providers={"default": FileStorageProvider("./data/storage")},
        membership_provider=YamlMembershipProvider("./data/membership.yaml"),
    )
    await silo.start()


if __name__ == "__main__":
    asyncio.run(main())
