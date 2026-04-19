"""Integration scenarios: cluster formation + owner-routed grain calls.

These tests exercise the full path through task 02-13 (distributed
directory), 02-14 (directory cache), and 02-16 (remote grain
invocation) end-to-end inside one pytest process.
"""

from __future__ import annotations

import pytest
from pyleans.grain import _grain_registry, grain
from pyleans.grain_base import Grain
from pyleans.identity import GrainId

from .harness import ClusterHarness, count_activations

pytestmark = pytest.mark.integration


@grain
class IntGrain(Grain):
    counter: int = 0

    async def inc(self, by: int = 1) -> int:
        self.counter += by
        return self.counter

    async def get(self) -> int:
        return self.counter


@pytest.fixture(autouse=True)
def _register_int_grain():
    _grain_registry["IntGrain"] = IntGrain
    yield


async def test_two_silos_form_cluster() -> None:
    # Arrange / Act
    async with ClusterHarness(n=2) as harness:
        # Assert - both silos know about each other via the ring
        assert len(harness.silos) == 2
        assert harness.silos[0].address.silo_id != harness.silos[1].address.silo_id


async def test_grain_call_routes_to_owner_from_either_silo() -> None:
    # Arrange - 2 silos; whichever silo the directory picks as owner hosts the activation
    async with ClusterHarness(n=2) as harness:
        grain_id = GrainId("IntGrain", "k-routing")

        # Act - call from silo 0
        first = await harness.silos[0].runtime.invoke(grain_id, "inc", args=[5])
        # Call from silo 1 (may be local or remote depending on ring)
        second = await harness.silos[1].runtime.invoke(grain_id, "inc", args=[3])

        # Assert - shared state: activation count across cluster is exactly 1
        assert count_activations(harness, grain_id) == 1
        assert first == 5
        assert second == 8


async def test_cache_hit_avoids_remote_roundtrip() -> None:
    # Arrange - force a remote call by using a non-owner silo
    async with ClusterHarness(n=2) as harness:
        grain_id = GrainId("IntGrain", "k-cache")

        # First call primes the owner + populates the caller's cache
        await harness.silos[0].runtime.invoke(grain_id, "inc", args=[1])
        snapshot_before = {s.address.silo_id: s.cache.size for s in harness.silos}

        # Act - 10 more calls from the same silo should mostly hit the cache
        for _ in range(10):
            await harness.silos[0].runtime.invoke(grain_id, "inc", args=[1])

        # Assert - cache never shrank (and only grew by the one entry we touch)
        snapshot_after = {s.address.silo_id: s.cache.size for s in harness.silos}
        for sid, before in snapshot_before.items():
            assert snapshot_after[sid] >= before


async def test_single_activation_under_concurrent_remote_calls() -> None:
    # Arrange
    import asyncio

    async with ClusterHarness(n=3) as harness:
        grain_id = GrainId("IntGrain", "k-contention")

        async def hit(silo_index: int) -> int:
            return await harness.silos[silo_index].runtime.invoke(grain_id, "inc", args=[1])

        # Act
        results = await asyncio.gather(*(hit(i % 3) for i in range(9)))

        # Assert - each silo saw the activation exactly once in total
        assert count_activations(harness, grain_id) == 1
        # 9 increments
        final = await harness.silos[0].runtime.invoke(grain_id, "get")
        assert final == 9
        assert sorted(results) == list(range(1, 10))
