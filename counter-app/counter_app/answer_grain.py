"""AnswerGrain — a minimal stateless grain that always returns 42."""

from pyleans import grain


@grain
class AnswerGrain:
    """A stateless grain that always returns 42.

    Demonstrates hosting multiple grains in one silo.
    """

    async def get(self) -> int:
        """Return the answer to everything."""
        return 42
