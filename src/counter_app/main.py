"""Standalone silo hosting CounterGrain."""

import asyncio

from pyleans.server import Silo
from pyleans.server.grains import system_grains
from pyleans.server.providers import FileStorageProvider, MarkdownTableMembershipProvider

from src.counter_app.answer_grain import AnswerGrain
from src.counter_app.counter_grain import CounterGrain


async def main() -> None:
    silo = Silo(
        grains=[CounterGrain, AnswerGrain, *system_grains()],
        storage_providers={"default": FileStorageProvider("./data/storage")},
        membership_provider=MarkdownTableMembershipProvider("./data/membership.md"),
    )
    await silo.start()


if __name__ == "__main__":
    asyncio.run(main())
