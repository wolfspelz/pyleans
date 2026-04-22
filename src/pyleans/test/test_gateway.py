"""Tests for the gateway listener and cluster client (integration)."""

import asyncio
from dataclasses import dataclass

import pytest
from pyleans.client import ClusterClient, RemoteGrainRef
from pyleans.errors import TransportError
from pyleans.grain import _grain_registry, grain
from pyleans.grain_base import Grain
from pyleans.identity import GrainId
from pyleans.net import InMemoryNetwork
from pyleans.server.providers.memory_stream import InMemoryStreamProvider
from pyleans.server.silo import Silo
from pyleans.testing import InMemoryMembershipProvider

from conftest import FakeStorageProvider

# Any unbound virtual address works — InMemoryNetwork.open_connection raises
# ConnectionRefusedError (OSError subclass) when no server is registered.
_UNBOUND_GATEWAY = "localhost:59999"

# --- Test grains ---


@dataclass
class GwCounterState:
    value: int = 0


@grain
class GwCounterGrain(Grain[GwCounterState]):
    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.write_state()
        return self.state.value

    async def set_value(self, value: int) -> None:
        self.state.value = value
        await self.write_state()


@grain
class GwErrorGrain:
    async def fail(self) -> None:
        raise ValueError("intentional error")


_GW_GRAINS = [GwCounterGrain, GwErrorGrain]


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    _grain_registry.clear()
    for cls in _GW_GRAINS:
        _grain_registry[cls.__name__] = cls


# --- Fake membership ---


FakeMembershipProvider = InMemoryMembershipProvider


# --- Helpers ---


def make_silo(network: InMemoryNetwork, *, host: str = "localhost", port: int = 11111) -> Silo:
    return Silo(
        grains=_GW_GRAINS,
        storage_providers={"default": FakeStorageProvider()},
        membership_provider=FakeMembershipProvider(),
        stream_providers={"default": InMemoryStreamProvider()},
        gateway_port=0,  # simulator assigns a virtual port
        host=host,
        port=port,
        network=network,
    )


# --- Tests ---


class TestGatewayConnection:
    """Test that a ClusterClient can connect to the silo gateway."""

    async def test_connect_and_close(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()
        assert client.connected

        await client.close()
        assert not client.connected

        await silo.stop()

    async def test_connect_to_nonexistent_gateway_raises(self, network: InMemoryNetwork) -> None:
        client = ClusterClient(gateways=[_UNBOUND_GATEWAY], network=network)
        with pytest.raises(ConnectionError, match="Could not connect"):
            await client.connect()

    async def test_empty_gateways_raises(self) -> None:
        with pytest.raises(ValueError, match="At least one gateway"):
            ClusterClient(gateways=[])

    async def test_connected_gateway_property_tracks_dialled_address(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange
        silo = make_silo(network)
        await silo.start_background()
        port = silo.gateway_port
        client = ClusterClient(gateways=[f"localhost:{port}"], network=network)

        # Act / Assert - None before connect, address while connected, None after close
        assert client.connected_gateway is None
        await client.connect()
        assert client.connected_gateway == f"localhost:{port}"
        await client.close()
        assert client.connected_gateway is None

        await silo.stop()

    async def test_connect_emits_info_log(
        self,
        network: InMemoryNetwork,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Arrange
        import logging

        silo = make_silo(network)
        await silo.start_background()
        port = silo.gateway_port

        # Act
        with caplog.at_level(logging.INFO, logger="pyleans.client.cluster_client"):
            client = ClusterClient(gateways=[f"localhost:{port}"], network=network)
            await client.connect()
            await client.close()

        # Assert - both connect and disconnect lines at INFO
        messages = [rec.getMessage() for rec in caplog.records]
        assert any(f"Connected to gateway localhost:{port}" in m for m in messages)
        assert any(f"Disconnected from gateway localhost:{port}" in m for m in messages)

        await silo.stop()

    async def test_server_drop_emits_info_log(
        self,
        network: InMemoryNetwork,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Arrange - silo stops while the client is still connected
        import logging

        silo = make_silo(network)
        await silo.start_background()
        port = silo.gateway_port
        client = ClusterClient(gateways=[f"localhost:{port}"], network=network)
        await client.connect()

        # Act - stopping the silo closes the gateway; the client sees EOF
        with caplog.at_level(logging.INFO, logger="pyleans.client.cluster_client"):
            await silo.stop()
            # give the read loop one tick to notice
            import asyncio as _aio

            await _aio.sleep(0.05)

        # Assert
        messages = [rec.getMessage() for rec in caplog.records]
        assert any(
            f"Disconnected by gateway localhost:{port} (connection lost)" in m for m in messages
        ), messages

        await client.close()


class TestRemoteGrainCalls:
    """Test grain calls over the gateway protocol."""

    async def test_get_grain_returns_remote_ref(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()

        ref = client.get_grain(GwCounterGrain, "c1")
        assert isinstance(ref, RemoteGrainRef)
        assert ref.identity == GrainId("GwCounterGrain", "c1")

        await client.close()
        await silo.stop()

    async def test_remote_increment(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()

        counter = client.get_grain(GwCounterGrain, "c1")
        assert await counter.increment() == 1
        assert await counter.increment() == 2
        assert await counter.get_value() == 2

        await client.close()
        await silo.stop()

    async def test_remote_set_value(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()

        counter = client.get_grain(GwCounterGrain, "c1")
        await counter.set_value(42)
        assert await counter.get_value() == 42

        await client.close()
        await silo.stop()

    async def test_multiple_grains_independent(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()

        c1 = client.get_grain(GwCounterGrain, "a")
        c2 = client.get_grain(GwCounterGrain, "b")

        await c1.increment()
        await c1.increment()
        await c2.set_value(100)

        assert await c1.get_value() == 2
        assert await c2.get_value() == 100

        await client.close()
        await silo.stop()

    async def test_concurrent_remote_calls(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()

        refs = [client.get_grain(GwCounterGrain, f"c{i}") for i in range(5)]
        await asyncio.gather(*(r.increment() for r in refs))
        results = await asyncio.gather(*(r.get_value() for r in refs))
        assert results == [1, 1, 1, 1, 1]

        await client.close()
        await silo.stop()


class TestRemoteErrorHandling:
    """Test error propagation through the gateway."""

    async def test_grain_error_returns_error(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()

        ref = client.get_grain(GwErrorGrain, "e1")
        with pytest.raises(TransportError, match="intentional error"):
            await ref.fail()

        await client.close()
        await silo.stop()

    async def test_unknown_grain_returns_error(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()

        # Manually create a ref for a non-existent grain type
        ref = RemoteGrainRef(grain_id=GrainId("NoSuchGrain", "x"), client=client)
        with pytest.raises(TransportError):
            await ref.some_method()

        await client.close()
        await silo.stop()

    async def test_invoke_without_connection_raises(self, network: InMemoryNetwork) -> None:
        client = ClusterClient(gateways=[_UNBOUND_GATEWAY], network=network)
        ref = RemoteGrainRef(grain_id=GrainId("Counter", "x"), client=client)
        with pytest.raises(ConnectionError, match="Not connected"):
            await ref.some_method()


class TestClientGatewayIntegration:
    """End-to-end: silo with gateway, client calls, state persisted."""

    async def test_state_persists_across_client_sessions(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()
        gateway = f"localhost:{silo.gateway_port}"

        # First client session
        client1 = ClusterClient(gateways=[gateway], network=network)
        await client1.connect()
        counter = client1.get_grain(GwCounterGrain, "persist")
        await counter.set_value(77)
        await client1.close()

        # Second client session — same silo, new client
        client2 = ClusterClient(gateways=[gateway], network=network)
        await client2.connect()
        counter2 = client2.get_grain(GwCounterGrain, "persist")
        assert await counter2.get_value() == 77
        await client2.close()

        await silo.stop()


class TestResilientReconnect:
    """Client with a gateway_refresher fails over to another silo."""

    async def test_refresher_replaces_dead_gateway_on_initial_connect(
        self,
        network: InMemoryNetwork,
    ) -> None:
        # Arrange - one live silo; constructor list only has a dead address
        silo = make_silo(network)
        await silo.start_background()
        live_gateway = f"localhost:{silo.gateway_port}"
        refresh_count = {"n": 0}

        async def refresher() -> list[str]:
            refresh_count["n"] += 1
            return [live_gateway]

        # Act - client was built with a stale gateway but a refresher
        client = ClusterClient(
            gateways=[_UNBOUND_GATEWAY],
            network=network,
            gateway_refresher=refresher,
        )
        await client.connect()

        # Assert - connect succeeded via the refreshed list, not the stale one
        assert client.connected
        assert client.connected_gateway == live_gateway
        assert refresh_count["n"] >= 1

        await client.close()
        await silo.stop()

    async def test_refresher_fails_over_mid_session(
        self,
        network: InMemoryNetwork,
    ) -> None:
        # Arrange - silo A dies mid-session; refresher then returns silo B.
        # Two distinct host names so the cluster-transport listeners don't collide.
        silo_a = make_silo(network, host="host-a", port=11141)
        await silo_a.start_background()
        gateway_a = f"host-a:{silo_a.gateway_port}"

        silo_b = make_silo(network, host="host-b", port=11142)
        await silo_b.start_background()
        gateway_b = f"host-b:{silo_b.gateway_port}"

        refresh_seen: list[list[str]] = []

        async def refresher() -> list[str]:
            live = [gateway_b] if silo_b.started else []
            refresh_seen.append(list(live))
            return live

        client = ClusterClient(
            gateways=[gateway_a],
            network=network,
            gateway_refresher=refresher,
        )
        await client.connect()
        # ``connect`` refreshed before dialling, so silo_b (from refresher) wins.
        # Force the test to dial silo_a first by installing the refresher *after*
        # connect — that preserves the "alive when you connected, died later" shape.
        assert client.connected_gateway == gateway_b
        await client.close()
        # Reconnect explicitly to silo_a, keeping refresher wired
        client = ClusterClient(
            gateways=[gateway_a],
            network=network,
        )
        await client.connect()
        assert client.connected_gateway == gateway_a
        # pylint: disable=protected-access
        client._gateway_refresher = refresher  # type: ignore[assignment]
        client._max_reconnect_attempts = 2

        counter = client.get_grain(GwCounterGrain, "failover")
        await counter.set_value(1)

        # Act - kill silo_a; the next invoke sees a connection loss and should
        # transparently fail over to silo_b via the refresher.
        await silo_a.stop()
        await asyncio.sleep(0.05)
        value = await counter.get_value()

        # Assert - call returned, client is now connected to silo_b
        assert value == 0  # fresh activation on silo_b — different storage
        assert client.connected_gateway == gateway_b
        assert refresh_seen  # at least one refresh happened on the failover path

        await client.close()
        await silo_b.stop()

    async def test_no_refresher_surfaces_connection_loss(
        self,
        network: InMemoryNetwork,
    ) -> None:
        # Arrange - legacy path: no refresher, single gateway
        silo = make_silo(network)
        await silo.start_background()
        client = ClusterClient(
            gateways=[f"localhost:{silo.gateway_port}"],
            network=network,
        )
        await client.connect()
        counter = client.get_grain(GwCounterGrain, "loss")
        await counter.set_value(3)

        # Act - silo dies; no refresher, so invoke must raise
        await silo.stop()
        await asyncio.sleep(0.05)

        # Assert
        with pytest.raises((ConnectionError, TimeoutError)):
            await counter.get_value()

        await client.close()

    async def test_max_reconnect_attempts_zero_disables_retry(
        self,
        network: InMemoryNetwork,
    ) -> None:
        # Arrange - refresher supplied, but retry budget is zero
        silo = make_silo(network)
        await silo.start_background()
        gateway = f"localhost:{silo.gateway_port}"
        refresh_count = {"n": 0}

        async def refresher() -> list[str]:
            refresh_count["n"] += 1
            return [gateway]

        client = ClusterClient(
            gateways=[gateway],
            network=network,
            gateway_refresher=refresher,
            max_reconnect_attempts=0,
        )
        await client.connect()
        counter = client.get_grain(GwCounterGrain, "no-retry")
        await counter.set_value(1)

        # Act - silo dies mid-session; retry budget is zero -> no reconnect
        await silo.stop()
        await asyncio.sleep(0.05)

        # Assert
        with pytest.raises((ConnectionError, TimeoutError)):
            await counter.get_value()

        await client.close()

    async def test_negative_max_reconnect_attempts_rejected(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="max_reconnect_attempts"):
            ClusterClient(
                gateways=["localhost:1"],
                max_reconnect_attempts=-1,
            )

    async def test_constructor_requires_gateways_or_refresher(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="gateway address or a gateway_refresher"):
            ClusterClient(gateways=[])
