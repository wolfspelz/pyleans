"""Tests for the gateway listener and cluster client (integration)."""

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import FakeStorageProvider
from pyleans.client import ClusterClient, RemoteGrainRef
from pyleans.errors import TransportError
from pyleans.grain import _grain_registry, grain
from pyleans.identity import GrainId, SiloStatus
from pyleans.providers.membership import MembershipProvider
from pyleans.server.providers.memory_stream import InMemoryStreamProvider
from pyleans.server.silo import Silo


# --- Test grains ---


@dataclass
class GwCounterState:
    value: int = 0


@grain(state_type=GwCounterState)
class GwCounterGrain:
    async def get_value(self) -> int:
        return self.state.value

    async def increment(self) -> int:
        self.state.value += 1
        await self.save_state()
        return self.state.value

    async def set_value(self, value: int) -> None:
        self.state.value = value
        await self.save_state()


@grain
class GwErrorGrain:
    async def fail(self) -> None:
        raise ValueError("intentional error")


_GW_GRAINS = [GwCounterGrain, GwErrorGrain]


@pytest.fixture(autouse=True)
def _reset_registry() -> None:  # type: ignore[misc]
    _grain_registry.clear()
    for cls in _GW_GRAINS:
        _grain_registry[cls.__name__] = cls


# --- Fake membership ---


class FakeMembershipProvider(MembershipProvider):
    def __init__(self) -> None:
        self.silos: dict[str, Any] = {}

    async def register_silo(self, silo: Any) -> None:
        self.silos[silo.address.encoded] = silo

    async def unregister_silo(self, silo_id: str) -> None:
        self.silos.pop(silo_id, None)

    async def get_active_silos(self) -> list[Any]:
        return list(self.silos.values())

    async def heartbeat(self, silo_id: str) -> None:
        pass

    async def update_status(self, silo_id: str, status: SiloStatus) -> None:
        pass


# --- Helpers ---


def make_silo() -> Silo:
    return Silo(
        grains=_GW_GRAINS,
        storage_providers={"default": FakeStorageProvider()},
        membership_provider=FakeMembershipProvider(),
        stream_providers={"default": InMemoryStreamProvider()},
        gateway_port=0,  # OS-assigned port
    )


# --- Tests ---


class TestGatewayConnection:
    """Test that a ClusterClient can connect to the silo gateway."""

    async def test_connect_and_close(self) -> None:
        silo = make_silo()
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"])
        await client.connect()
        assert client.connected

        await client.close()
        assert not client.connected

        await silo.stop()

    async def test_connect_to_nonexistent_gateway_raises(self) -> None:
        client = ClusterClient(gateways=["localhost:59999"])
        with pytest.raises(ConnectionError, match="Could not connect"):
            await client.connect()

    async def test_empty_gateways_raises(self) -> None:
        with pytest.raises(ValueError, match="At least one gateway"):
            ClusterClient(gateways=[])


class TestRemoteGrainCalls:
    """Test grain calls over the gateway protocol."""

    async def test_get_grain_returns_remote_ref(self) -> None:
        silo = make_silo()
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"])
        await client.connect()

        ref = client.get_grain(GwCounterGrain, "c1")
        assert isinstance(ref, RemoteGrainRef)
        assert ref.identity == GrainId("GwCounterGrain", "c1")

        await client.close()
        await silo.stop()

    async def test_remote_increment(self) -> None:
        silo = make_silo()
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"])
        await client.connect()

        counter = client.get_grain(GwCounterGrain, "c1")
        assert await counter.increment() == 1
        assert await counter.increment() == 2
        assert await counter.get_value() == 2

        await client.close()
        await silo.stop()

    async def test_remote_set_value(self) -> None:
        silo = make_silo()
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"])
        await client.connect()

        counter = client.get_grain(GwCounterGrain, "c1")
        await counter.set_value(42)
        assert await counter.get_value() == 42

        await client.close()
        await silo.stop()

    async def test_multiple_grains_independent(self) -> None:
        silo = make_silo()
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"])
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

    async def test_concurrent_remote_calls(self) -> None:
        silo = make_silo()
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"])
        await client.connect()

        refs = [client.get_grain(GwCounterGrain, f"c{i}") for i in range(5)]
        await asyncio.gather(*(r.increment() for r in refs))
        results = await asyncio.gather(*(r.get_value() for r in refs))
        assert results == [1, 1, 1, 1, 1]

        await client.close()
        await silo.stop()


class TestRemoteErrorHandling:
    """Test error propagation through the gateway."""

    async def test_grain_error_returns_error(self) -> None:
        silo = make_silo()
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"])
        await client.connect()

        ref = client.get_grain(GwErrorGrain, "e1")
        with pytest.raises(TransportError, match="intentional error"):
            await ref.fail()

        await client.close()
        await silo.stop()

    async def test_unknown_grain_returns_error(self) -> None:
        silo = make_silo()
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"])
        await client.connect()

        # Manually create a ref for a non-existent grain type
        ref = RemoteGrainRef(
            grain_id=GrainId("NoSuchGrain", "x"), client=client
        )
        with pytest.raises(TransportError):
            await ref.some_method()

        await client.close()
        await silo.stop()

    async def test_invoke_without_connection_raises(self) -> None:
        client = ClusterClient(gateways=["localhost:59999"])
        ref = RemoteGrainRef(
            grain_id=GrainId("Counter", "x"), client=client
        )
        with pytest.raises(ConnectionError, match="Not connected"):
            await ref.some_method()


class TestClientGatewayIntegration:
    """End-to-end: silo with gateway, client calls, state persisted."""

    async def test_state_persists_across_client_sessions(self) -> None:
        silo = make_silo()
        await silo.start_background()
        gateway = f"localhost:{silo.gateway_port}"

        # First client session
        client1 = ClusterClient(gateways=[gateway])
        await client1.connect()
        counter = client1.get_grain(GwCounterGrain, "persist")
        await counter.set_value(77)
        await client1.close()

        # Second client session — same silo, new client
        client2 = ClusterClient(gateways=[gateway])
        await client2.connect()
        counter2 = client2.get_grain(GwCounterGrain, "persist")
        assert await counter2.get_value() == 77
        await client2.close()

        await silo.stop()
