"""Minimal N-silo in-process cluster harness.

Each harness "silo" is a hand-assembled composition of the Phase 2
building blocks:

* an in-memory fabric acting as the :class:`IClusterTransport`,
* a :class:`DistributedGrainDirectory` fronted by a
  :class:`DirectoryCache`,
* a :class:`GrainRuntime` with the directory cache injected.

The harness intentionally does not spin up the real `Silo` class —
Silo's own refactor to lifecycle-driven startup is deferred (see
task 02-17). This lets us exercise the interactions task 02-18
cares about without blocking on Silo surgery, and still gives the
13-scenario catalog a solid starting point.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pyleans.cluster.directory_cache import DirectoryCache
from pyleans.cluster.distributed_directory import DistributedGrainDirectory
from pyleans.cluster.hash_ring import ConsistentHashRing
from pyleans.cluster.identity import ClusterId
from pyleans.cluster.placement import PreferLocalPlacement
from pyleans.identity import GrainId, SiloAddress
from pyleans.serialization import JsonSerializer
from pyleans.server.remote_invoke import GRAIN_CALL_HEADER
from pyleans.server.runtime import GrainRuntime
from pyleans.transport.cluster import (
    ConnectionCallback,
    DisconnectionCallback,
    IClusterTransport,
    MessageHandler,
)
from pyleans.transport.errors import TransportConnectionError
from pyleans.transport.messages import MessageType, TransportMessage

logger = logging.getLogger(__name__)


@dataclass
class _InMemoryFabric:
    """In-process routing fabric: dispatches frames to each silo's handler."""

    handlers: dict[str, MessageHandler] = field(default_factory=dict)
    dropped_pairs: set[tuple[str, str]] = field(default_factory=set)
    correlation_counter: int = 0

    def partition(self, a: str, b: str) -> None:
        self.dropped_pairs.add((a, b))
        self.dropped_pairs.add((b, a))

    def heal(self, a: str, b: str) -> None:
        self.dropped_pairs.discard((a, b))
        self.dropped_pairs.discard((b, a))

    def is_dropped(self, source: str, target: str) -> bool:
        return (source, target) in self.dropped_pairs

    def register(self, silo_id: str, handler: MessageHandler) -> None:
        self.handlers[silo_id] = handler

    def unregister(self, silo_id: str) -> None:
        self.handlers.pop(silo_id, None)

    def next_correlation(self) -> int:
        self.correlation_counter += 1
        return self.correlation_counter


@dataclass
class _FabricTransport(IClusterTransport):
    """IClusterTransport adapter over :class:`_InMemoryFabric`."""

    local: SiloAddress
    fabric: _InMemoryFabric
    connected: set[str] = field(default_factory=set)

    async def start(
        self,
        local_silo: SiloAddress,
        cluster_id: ClusterId,
        message_handler: MessageHandler,
    ) -> None:
        del local_silo, cluster_id
        self.fabric.register(self.local.silo_id, message_handler)

    async def stop(self) -> None:
        self.fabric.unregister(self.local.silo_id)

    async def connect_to_silo(self, silo: SiloAddress) -> None:
        self.connected.add(silo.silo_id)

    async def disconnect_from_silo(self, silo: SiloAddress) -> None:
        self.connected.discard(silo.silo_id)

    async def send_request(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        del timeout
        if self.fabric.is_dropped(self.local.silo_id, target.silo_id):
            raise TransportConnectionError(
                f"partition: {self.local.silo_id} -> {target.silo_id} dropped",
            )
        handler = self.fabric.handlers.get(target.silo_id)
        if handler is None:
            raise TransportConnectionError(f"silo {target.silo_id} not registered")
        msg = TransportMessage(
            message_type=MessageType.REQUEST,
            correlation_id=self.fabric.next_correlation(),
            header=header,
            body=body,
        )
        resp = await handler(self.local, msg)
        if resp is None:
            raise TransportConnectionError(f"{target.silo_id} returned no response")
        return resp.header, resp.body

    async def send_one_way(self, target: SiloAddress, header: bytes, body: bytes) -> None:
        if self.fabric.is_dropped(self.local.silo_id, target.silo_id):
            return
        handler = self.fabric.handlers.get(target.silo_id)
        if handler is None:
            return
        msg = TransportMessage(
            message_type=MessageType.ONE_WAY,
            correlation_id=0,
            header=header,
            body=body,
        )
        await handler(self.local, msg)

    async def send_ping(self, target: SiloAddress, timeout: float = 10.0) -> float:
        del target, timeout
        return 0.001

    def is_connected_to(self, silo: SiloAddress) -> bool:
        return silo.silo_id in self.connected

    def get_connected_silos(self) -> list[SiloAddress]:
        return []

    @property
    def local_silo(self) -> SiloAddress:
        return self.local

    def on_connection_established(self, callback: ConnectionCallback) -> None:
        pass

    def on_connection_lost(self, callback: DisconnectionCallback) -> None:
        pass


@dataclass
class HarnessSilo:
    """A minimal cluster member assembled from Phase 2 primitives."""

    address: SiloAddress
    transport: _FabricTransport
    directory: DistributedGrainDirectory
    cache: DirectoryCache
    runtime: GrainRuntime

    async def start(self, fabric: _InMemoryFabric) -> None:
        await self.transport.start(self.address, ClusterId("int-cluster"), self._handler)
        fabric.register(self.address.silo_id, self._handler)

    async def stop(self) -> None:
        await self.runtime.stop()
        await self.transport.stop()

    async def _handler(self, source: SiloAddress, msg: TransportMessage) -> TransportMessage | None:
        if msg.header == GRAIN_CALL_HEADER:
            return await self.runtime.handle_grain_call(source, msg)
        return await self.directory.message_handler(source, msg)


class ClusterHarness:
    """Spin up N silos in one process over an in-memory fabric.

    Use as an async context manager::

        async with ClusterHarness(n=3) as harness:
            result = await harness.silos[0].runtime.invoke(...)
    """

    def __init__(self, n: int, *, host_prefix: str = "sim") -> None:
        if n < 1:
            raise ValueError(f"n must be >= 1, got {n!r}")
        self._n = n
        self._host_prefix = host_prefix
        self._fabric = _InMemoryFabric()
        self._silos: list[HarnessSilo] = []

    @property
    def fabric(self) -> _InMemoryFabric:
        return self._fabric

    @property
    def silos(self) -> list[HarnessSilo]:
        return list(self._silos)

    async def __aenter__(self) -> ClusterHarness:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        addresses = [
            SiloAddress(host=f"{self._host_prefix}{i}", port=11111, epoch=1) for i in range(self._n)
        ]
        ring = ConsistentHashRing(addresses)
        for address in addresses:
            transport = _FabricTransport(local=address, fabric=self._fabric)
            directory = DistributedGrainDirectory(
                local_silo=address,
                transport=transport,
                initial_ring=ring,
                active_silos=list(addresses),
                rebuild_grace_period=0.0,
            )
            cache = DirectoryCache(directory)
            runtime = GrainRuntime(
                storage_providers={},
                serializer=JsonSerializer(),
                directory=cache,
                local_silo=address,
                placement_strategy=PreferLocalPlacement(),
                transport=transport,
            )
            silo = HarnessSilo(
                address=address,
                transport=transport,
                directory=directory,
                cache=cache,
                runtime=runtime,
            )
            await silo.start(self._fabric)
            self._silos.append(silo)

    async def stop(self) -> None:
        for silo in self._silos:
            try:
                await silo.stop()
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("harness stop of %s failed: %r", silo.address.silo_id, exc)
        self._silos.clear()

    async def partition(self, a: int, b: int) -> None:
        self._fabric.partition(self._silos[a].address.silo_id, self._silos[b].address.silo_id)

    async def heal_partition(self, a: int, b: int) -> None:
        self._fabric.heal(self._silos[a].address.silo_id, self._silos[b].address.silo_id)


async def wait_until(
    predicate: Callable[[], Awaitable[bool]],
    timeout: float = 5.0,
    interval: float = 0.01,
) -> None:
    """Poll ``predicate`` every ``interval`` seconds until it returns True or ``timeout``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await predicate():
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


def count_activations(harness: ClusterHarness, grain_id: GrainId) -> int:
    """Count, across the cluster, how many silos have the grain in their runtime."""
    total = 0
    for silo in harness.silos:
        if grain_id in silo.runtime.activations:
            total += 1
    return total
