"""Integration scenarios: connection loss triggers cache invalidation + retry.

The harness's fabric-level partition injection lets us simulate a
torn connection between two silos while the rest of the cluster
stays intact. The runtime should catch the error, invalidate its
cache for the affected grain, re-resolve through the directory, and
try again.
"""

from __future__ import annotations

import pytest
from pyleans.grain import _grain_registry, grain
from pyleans.grain_base import Grain
from pyleans.identity import GrainId

from .harness import ClusterHarness

pytestmark = pytest.mark.integration


@grain
class FailGrain(Grain):
    counter: int = 0

    async def inc(self) -> int:
        self.counter += 1
        return self.counter


@pytest.fixture(autouse=True)
def _register_fail_grain():
    _grain_registry["FailGrain"] = FailGrain
    yield


async def test_partition_between_owner_and_caller_triggers_cache_invalidate() -> None:
    # Arrange - 3 silos; activate a grain, partition caller ↔ owner
    async with ClusterHarness(n=3) as harness:
        grain_id = GrainId("FailGrain", "k-partition")

        # Seed the grain so it has a definite owner
        await harness.silos[0].runtime.invoke(grain_id, "inc")

        # Find which silo owns it
        owner_entry = await harness.silos[0].cache.lookup(grain_id)
        assert owner_entry is not None
        owner_index = next(
            i for i, s in enumerate(harness.silos) if s.address.silo_id == owner_entry.silo.silo_id
        )
        # Pick a caller that is not the owner
        caller_index = next(i for i in range(3) if i != owner_index)

        # Act - partition caller ↔ owner, verify it raises or reroutes
        await harness.partition(caller_index, owner_index)

        # The runtime catches TransportConnectionError, invalidates the cache,
        # re-resolves. If the owner's partition didn't move (ring unchanged),
        # the retry still targets the partitioned owner and raises.
        from pyleans.transport.errors import TransportConnectionError

        with pytest.raises(TransportConnectionError):
            await harness.silos[caller_index].runtime.invoke(grain_id, "inc")

        # Assert - the cache for that grain was invalidated on the caller
        assert not harness.silos[caller_index].cache.contains(grain_id)
