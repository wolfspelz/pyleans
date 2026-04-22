"""N-silo in-process cluster harness built on the real :class:`Silo`.

Each harness silo is a production ``Silo`` instance bound to a shared
:class:`InMemoryNetwork` — the same network every other silo in the
harness binds on — so silo-to-silo transport, gateway traffic, and
membership writes all flow through deterministic in-process streams
(no OS ports).

Partition injection wraps the shared network per-silo so sends that
would otherwise cross a broken link fail at :meth:`open_connection`
with :class:`ConnectionRefusedError`. The existing connection is torn
down explicitly so a partition introduced after the handshake
disrupts in-flight requests rather than waiting for the next
reconnect.
"""

from __future__ import annotations

import asyncio
import logging
import ssl as ssl_module
from collections.abc import Awaitable, Callable
from typing import Any

from pyleans.cluster.identity import ClusterId
from pyleans.identity import GrainId
from pyleans.net.memory_network import InMemoryNetwork
from pyleans.net.network import (
    ClientConnectedCallback,
    INetwork,
    NetworkServer,
)
from pyleans.server.silo import Silo
from pyleans.testing import InMemoryMembershipProvider

logger = logging.getLogger(__name__)

_HARNESS_CLUSTER_ID = ClusterId("int-cluster")


class _PartitionRegistry:
    """Shared source-to-target block list used by all harness silos."""

    def __init__(self) -> None:
        self._blocked: set[tuple[str, str]] = set()
        self._address_to_silo: dict[tuple[str, int], str] = {}

    def register_silo(self, silo_id: str, host: str, port: int) -> None:
        self._address_to_silo[(host, port)] = silo_id

    def unregister_silo(self, host: str, port: int) -> None:
        self._address_to_silo.pop((host, port), None)

    def block(self, source_silo_id: str, target_silo_id: str) -> None:
        self._blocked.add((source_silo_id, target_silo_id))

    def unblock(self, source_silo_id: str, target_silo_id: str) -> None:
        self._blocked.discard((source_silo_id, target_silo_id))

    def is_blocked(self, source_silo_id: str, host: str, port: int) -> bool:
        target = self._address_to_silo.get((host, port))
        if target is None:
            return False
        return (source_silo_id, target) in self._blocked


class _PartitionedNetwork(INetwork):
    """Per-silo view over a shared :class:`INetwork` that enforces partitions.

    ``start_server`` passes through unchanged; ``open_connection``
    consults the shared :class:`_PartitionRegistry` keyed on the
    silo opening the connection.
    """

    def __init__(
        self,
        inner: INetwork,
        registry: _PartitionRegistry,
        local_silo_id: str,
    ) -> None:
        self._inner = inner
        self._registry = registry
        self._local = local_silo_id

    async def start_server(
        self,
        client_connected_cb: ClientConnectedCallback,
        host: str,
        port: int,
        *,
        ssl: ssl_module.SSLContext | None = None,
    ) -> NetworkServer:
        return await self._inner.start_server(client_connected_cb, host, port, ssl=ssl)

    async def open_connection(
        self,
        host: str,
        port: int,
        *,
        ssl: ssl_module.SSLContext | None = None,
    ) -> Any:
        if self._registry.is_blocked(self._local, host, port):
            raise ConnectionRefusedError(
                f"harness partition: {self._local} -> {host}:{port}",
            )
        return await self._inner.open_connection(host, port, ssl=ssl)


class ClusterHarness:
    """Spin up N real :class:`Silo` instances over one :class:`InMemoryNetwork`.

    Use as an async context manager::

        async with ClusterHarness(n=3) as harness:
            result = await harness.silos[0].runtime.invoke(...)
    """

    def __init__(
        self,
        n: int,
        *,
        grains: list[type] | None = None,
        host_prefix: str = "sim",
        base_port: int = 11111,
    ) -> None:
        if n < 1:
            raise ValueError(f"n must be >= 1, got {n!r}")
        self._n = n
        self._grains = grains or []
        self._host_prefix = host_prefix
        self._base_port = base_port
        self._network = InMemoryNetwork()
        self._registry = _PartitionRegistry()
        self._membership = InMemoryMembershipProvider()
        self._silos: list[Silo] = []

    @property
    def silos(self) -> list[Silo]:
        return list(self._silos)

    @property
    def network(self) -> InMemoryNetwork:
        return self._network

    @property
    def membership(self) -> InMemoryMembershipProvider:
        return self._membership

    async def __aenter__(self) -> ClusterHarness:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        """Construct N Silo instances on the shared network and start them all."""
        for i in range(self._n):
            silo = self._build_silo(index=i)
            self._silos.append(silo)
        await asyncio.gather(*(silo.start_background() for silo in self._silos))
        await self._seed_membership_awareness()

    async def stop(self) -> None:
        """Stop all silos; failures are logged, not raised."""
        results = await asyncio.gather(
            *(silo.stop() for silo in self._silos),
            return_exceptions=True,
        )
        for silo, res in zip(self._silos, results, strict=True):
            if isinstance(res, BaseException):
                logger.warning("Harness stop of %s failed: %r", silo.address.silo_id, res)
            self._registry.unregister_silo(silo.address.host, silo.address.port)
        self._silos.clear()

    async def partition(self, a: int, b: int) -> None:
        """Block future sends between silos[a] and silos[b] in both directions."""
        silo_a = self._silos[a]
        silo_b = self._silos[b]
        self._registry.block(silo_a.address.silo_id, silo_b.address.silo_id)
        self._registry.block(silo_b.address.silo_id, silo_a.address.silo_id)
        await asyncio.gather(
            silo_a.cluster_transport.disconnect_from_silo(silo_b.address),
            silo_b.cluster_transport.disconnect_from_silo(silo_a.address),
            return_exceptions=True,
        )

    async def heal_partition(self, a: int, b: int) -> None:
        """Clear a previously-applied partition between silos[a] and silos[b]."""
        silo_a = self._silos[a]
        silo_b = self._silos[b]
        self._registry.unblock(silo_a.address.silo_id, silo_b.address.silo_id)
        self._registry.unblock(silo_b.address.silo_id, silo_a.address.silo_id)

    def _build_silo(self, index: int) -> Silo:
        host = f"{self._host_prefix}{index}"
        port = self._base_port + index
        # Fixed epoch keeps the hash ring deterministic across test runs.
        epoch = 1
        silo_id = f"{host}:{port}:{epoch}"
        self._registry.register_silo(silo_id, host, port)
        partitioned = _PartitionedNetwork(
            inner=self._network,
            registry=self._registry,
            local_silo_id=silo_id,
        )
        return Silo(
            grains=self._grains,
            membership_provider=self._membership,
            host=host,
            port=port,
            gateway_port=0,
            cluster_id=_HARNESS_CLUSTER_ID,
            network=partitioned,
            epoch=epoch,
        )

    async def _seed_membership_awareness(self) -> None:
        """Trigger a membership snapshot broadcast so every silo sees every peer.

        The membership agent's normal flow picks up peers on the next
        ``i_am_alive`` cycle; integration tests need it instantly so
        the first grain call already routes over the real ring.
        """
        snapshot = await self._membership.read_all()
        active = [s.address for s in snapshot.silos]
        for silo in self._silos:
            # pylint: disable=protected-access
            await silo._directory.on_membership_changed(active)
            silo._cache.invalidate_all()
        # Open outbound connections so subsequent directory RPCs don't spend
        # their first request on a cold handshake.
        await asyncio.gather(
            *(self._connect_all_peers(silo) for silo in self._silos),
            return_exceptions=True,
        )

    async def _connect_all_peers(self, silo: Silo) -> None:
        for peer in self._silos:
            if peer is silo:
                continue
            try:
                await silo.cluster_transport.connect_to_silo(peer.address)
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug(
                    "initial connect %s -> %s raised %r",
                    silo.address.silo_id,
                    peer.address.silo_id,
                    exc,
                )


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
