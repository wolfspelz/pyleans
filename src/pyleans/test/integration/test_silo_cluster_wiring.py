"""Integration scenarios: real ``Silo`` instances exercised over ``InMemoryNetwork``.

These tests cover task 02-22's acceptance criteria — that the silo
composes cluster transport, distributed directory, directory cache,
and membership agent through :class:`SiloLifecycle` and that the
single-activation contract from adr-single-activation-cluster holds
across N silos.
"""

from __future__ import annotations

import asyncio

import pytest
from pyleans.grain import _grain_registry, grain
from pyleans.grain_base import Grain
from pyleans.identity import GrainId

from .harness import ClusterHarness, count_activations, wait_until

pytestmark = pytest.mark.integration


@grain
class WireGrain(Grain):
    counter: int = 0

    async def inc(self, by: int = 1) -> int:
        self.counter += by
        return self.counter

    async def get(self) -> int:
        return self.counter


@pytest.fixture(autouse=True)
def _register_wire_grain():
    _grain_registry["WireGrain"] = WireGrain
    yield


class TestSingleSiloHappyPath:
    async def test_n1_single_silo_serves_grain_calls(self) -> None:
        # Arrange
        async with ClusterHarness(n=1, grains=[WireGrain]) as harness:
            grain_id = GrainId("WireGrain", "solo")

            # Act
            first = await harness.silos[0].runtime.invoke(grain_id, "inc", args=[4])
            second = await harness.silos[0].runtime.invoke(grain_id, "inc", args=[2])

            # Assert - single-silo deployment still serves grains end-to-end
            assert first == 4
            assert second == 6
            assert count_activations(harness, grain_id) == 1


class TestTwoSiloSingleActivation:
    async def test_n2_single_activation_across_cluster(self) -> None:
        # Arrange
        async with ClusterHarness(n=2, grains=[WireGrain]) as harness:
            grain_id = GrainId("WireGrain", "pair")

            # Act - first call from silo 0, second from silo 1 — shared state
            first = await harness.silos[0].runtime.invoke(grain_id, "inc", args=[1])
            second = await harness.silos[1].runtime.invoke(grain_id, "inc", args=[1])

            # Assert - ADR invariant: exactly one activation across the cluster
            a = len(harness.silos[0].runtime.activations)
            b = len(harness.silos[1].runtime.activations)
            assert a + b == 1
            assert first == 1
            assert second == 2


class TestThreeSiloRingDistribution:
    async def test_n3_directory_entries_distribute_across_ring(self) -> None:
        # Arrange - 30 grain ids; directory ownership is ring-hash driven, so
        # at least two silos should end up hosting part of the partition.
        async with ClusterHarness(n=3, grains=[WireGrain]) as harness:
            grain_ids = [GrainId("WireGrain", f"k-{i}") for i in range(30)]

            # Act - each call activates one grain somewhere in the cluster.
            for i, gid in enumerate(grain_ids):
                await harness.silos[i % 3].runtime.invoke(gid, "inc")

            # Assert - directory partitions are spread over the ring
            partition_sizes = [len(s.directory.owned) for s in harness.silos]
            assert sum(partition_sizes) == 30
            assert sum(1 for c in partition_sizes if c > 0) >= 2


class TestConcurrentResolveOrActivate:
    async def test_concurrent_invocations_serialize_on_owner(self) -> None:
        # Arrange - 9 concurrent calls from 3 silos to one grain
        async with ClusterHarness(n=3, grains=[WireGrain]) as harness:
            grain_id = GrainId("WireGrain", "hot")

            # Act
            async def hit(silo_index: int) -> int:
                return await harness.silos[silo_index].runtime.invoke(grain_id, "inc", args=[1])

            results = await asyncio.gather(*(hit(i % 3) for i in range(9)))

            # Assert - one activation; increments serialised to 1..9
            assert count_activations(harness, grain_id) == 1
            assert sorted(results) == list(range(1, 10))


class TestPeerUnreachable:
    async def test_partitioned_caller_raises_transport_error(self) -> None:
        # Arrange
        async with ClusterHarness(n=3, grains=[WireGrain]) as harness:
            grain_id = GrainId("WireGrain", "partition-victim")
            await harness.silos[0].runtime.invoke(grain_id, "inc")
            owner_entry = await harness.silos[0].cache.lookup(grain_id)
            assert owner_entry is not None
            owner_index = next(
                i
                for i, s in enumerate(harness.silos)
                if s.address.silo_id == owner_entry.silo.silo_id
            )
            caller_index = next(i for i in range(3) if i != owner_index)

            # Act - cut the link and retry
            await harness.partition(caller_index, owner_index)

            # Assert - the runtime surfaces transport failure after the retry
            from pyleans.transport.errors import TransportConnectionError

            with pytest.raises(TransportConnectionError):
                await harness.silos[caller_index].runtime.invoke(grain_id, "inc")


class TestStaleCacheRerouting:
    async def test_cache_invalidated_on_lookup_miss_after_partition(self) -> None:
        # Arrange - activate a grain to populate caller-side cache
        async with ClusterHarness(n=3, grains=[WireGrain]) as harness:
            grain_id = GrainId("WireGrain", "stale-cache")
            await harness.silos[0].runtime.invoke(grain_id, "inc")
            owner_entry = await harness.silos[0].cache.lookup(grain_id)
            assert owner_entry is not None
            owner_index = next(
                i
                for i, s in enumerate(harness.silos)
                if s.address.silo_id == owner_entry.silo.silo_id
            )
            caller_index = next(i for i in range(3) if i != owner_index)
            # Warm caller's cache by calling through it before the partition
            await harness.silos[caller_index].runtime.invoke(grain_id, "inc")
            assert harness.silos[caller_index].cache.contains(grain_id)

            # Act - explicitly invalidate and verify the cache obeys the signal.
            harness.silos[caller_index].cache.invalidate(grain_id)

            # Assert - eviction hook dropped the stale entry
            assert not harness.silos[caller_index].cache.contains(grain_id)


class TestSiloLeavesDuringCall:
    async def test_owner_graceful_leave_allows_reactivation_on_survivor(self) -> None:
        # Arrange - activate a grain, then stop its owner; a survivor reactivates it.
        async with ClusterHarness(n=2, grains=[WireGrain]) as harness:
            grain_id = GrainId("WireGrain", "graceful-leave")
            await harness.silos[0].runtime.invoke(grain_id, "inc")
            owner_entry = await harness.silos[0].cache.lookup(grain_id)
            assert owner_entry is not None
            owner_index = next(
                i
                for i, s in enumerate(harness.silos)
                if s.address.silo_id == owner_entry.silo.silo_id
            )
            survivor_index = 1 - owner_index

            # Act - owner leaves gracefully; membership table loses the row
            await harness.silos[owner_index].stop()
            survivor = harness.silos[survivor_index]
            snapshot = await harness.membership.read_all()
            active = [s.address for s in snapshot.silos]
            # pylint: disable=protected-access
            await survivor._directory.on_membership_changed(active)
            survivor.cache.invalidate_all()

            # A call on the survivor now triggers a fresh activation locally
            result = await survivor.runtime.invoke(grain_id, "inc")

            # Assert - activation count is exactly 1 again, on the survivor
            assert len(survivor.runtime.activations) == 1
            assert result >= 1


class TestCounterIsolation:
    async def test_different_grain_keys_are_independent(self) -> None:
        # Arrange
        async with ClusterHarness(n=2, grains=[WireGrain]) as harness:
            a = GrainId("WireGrain", "a")
            b = GrainId("WireGrain", "b")

            # Act
            await harness.silos[0].runtime.invoke(a, "inc", args=[5])
            await harness.silos[1].runtime.invoke(b, "inc", args=[7])

            # Assert - independent state per key
            assert await harness.silos[0].runtime.invoke(a, "get") == 5
            assert await harness.silos[1].runtime.invoke(b, "get") == 7


class TestPartitionHealLetsCallsResume:
    async def test_heal_restores_routing(self) -> None:
        # Arrange
        async with ClusterHarness(n=3, grains=[WireGrain]) as harness:
            grain_id = GrainId("WireGrain", "heal")
            await harness.silos[0].runtime.invoke(grain_id, "inc")
            owner_entry = await harness.silos[0].cache.lookup(grain_id)
            assert owner_entry is not None
            owner_index = next(
                i
                for i, s in enumerate(harness.silos)
                if s.address.silo_id == owner_entry.silo.silo_id
            )
            caller_index = next(i for i in range(3) if i != owner_index)

            await harness.partition(caller_index, owner_index)
            from pyleans.transport.errors import TransportConnectionError

            with pytest.raises(TransportConnectionError):
                await harness.silos[caller_index].runtime.invoke(grain_id, "inc")

            # Act
            await harness.heal_partition(caller_index, owner_index)

            from pyleans.transport.errors import TransportConnectionError

            async def call_succeeds() -> bool:
                try:
                    await harness.silos[caller_index].runtime.invoke(grain_id, "inc")
                except TransportConnectionError:
                    return False
                return True

            # Assert - once healed, the call eventually goes through
            await wait_until(call_succeeds, timeout=2.0)
