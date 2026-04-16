"""Tests for SiloManagement service and system_grains() helper."""

import platform
import socket
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import FakeStorageProvider
from pyleans.grain import _grain_registry, grain
from pyleans.identity import GrainId, SiloStatus
from pyleans.providers.membership import MembershipProvider
from pyleans.server.grains import system_grains
from pyleans.server.providers.memory_stream import InMemoryStreamProvider
from pyleans.server.silo import Silo


# --- Test grain that exposes silo management ---


@dataclass
class MgmtCounterState:
    value: int = 0


@grain(state_type=MgmtCounterState)
class MgmtCounterGrain:
    async def get_value(self) -> int:
        return self.state.value  # type: ignore[attr-defined]

    async def increment(self) -> int:
        self.state.value += 1  # type: ignore[attr-defined]
        await self.save_state()  # type: ignore[attr-defined]
        return self.state.value  # type: ignore[attr-defined]

    async def get_silo_info(self) -> dict[str, Any]:
        return self.silo_management.get_info()  # type: ignore[attr-defined]


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


_TEST_GRAINS = [MgmtCounterGrain]


@pytest.fixture(autouse=True)
def _reset_registry() -> None:  # type: ignore[misc]
    _grain_registry.clear()
    for cls in _TEST_GRAINS:
        _grain_registry[cls.__name__] = cls


def make_silo() -> Silo:
    return Silo(
        grains=_TEST_GRAINS,
        storage_providers={"default": FakeStorageProvider()},
        membership_provider=FakeMembershipProvider(),
        stream_providers={"default": InMemoryStreamProvider()},
        gateway_port=0,
    )


# --- Tests ---


class TestSystemGrains:
    def test_returns_list(self) -> None:
        result = system_grains()
        assert isinstance(result, list)

    def test_currently_empty(self) -> None:
        assert system_grains() == []

    def test_can_spread_into_grain_list(self) -> None:
        grains = [MgmtCounterGrain, *system_grains()]
        assert MgmtCounterGrain in grains


class TestSiloManagementBinding:
    async def test_silo_management_bound_to_grain(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(MgmtCounterGrain, "c1")
        info = await ref.get_silo_info()
        assert isinstance(info, dict)
        assert "silo_id" in info

        await silo.stop()

    async def test_silo_management_accessible_from_any_grain(self) -> None:
        silo = make_silo()
        await silo.start_background()

        c1 = silo.grain_factory.get_grain(MgmtCounterGrain, "a")
        c2 = silo.grain_factory.get_grain(MgmtCounterGrain, "b")

        info1 = await c1.get_silo_info()
        info2 = await c2.get_silo_info()
        assert info1["silo_id"] == info2["silo_id"]

        await silo.stop()


class TestSiloManagementGetInfo:
    async def test_returns_dict(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(MgmtCounterGrain, "c1")
        info = await ref.get_silo_info()
        assert isinstance(info, dict)

        await silo.stop()

    async def test_all_keys_present(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(MgmtCounterGrain, "c1")
        info = await ref.get_silo_info()

        expected_keys = {
            "silo_id", "host", "hostname", "platform", "port",
            "gateway_port", "epoch", "status", "uptime_seconds",
            "grain_count", "idle_timeout",
        }
        assert set(info.keys()) == expected_keys

        await silo.stop()

    async def test_silo_id(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(MgmtCounterGrain, "c1")
        info = await ref.get_silo_info()
        assert info["silo_id"] == silo._silo_id

        await silo.stop()

    async def test_host_and_port(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(MgmtCounterGrain, "c1")
        info = await ref.get_silo_info()
        assert info["host"] == "localhost"
        assert info["port"] == 11111
        assert isinstance(info["gateway_port"], int)

        await silo.stop()

    async def test_hostname_and_platform(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(MgmtCounterGrain, "c1")
        info = await ref.get_silo_info()
        assert info["hostname"] == socket.gethostname()
        assert info["platform"] == platform.system()

        await silo.stop()

    async def test_status_active(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(MgmtCounterGrain, "c1")
        info = await ref.get_silo_info()
        assert info["status"] == "active"

        await silo.stop()

    async def test_uptime_positive(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(MgmtCounterGrain, "c1")
        info = await ref.get_silo_info()
        assert info["uptime_seconds"] >= 0

        await silo.stop()

    async def test_grain_count(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(MgmtCounterGrain, "c1")
        await ref.get_value()  # activate grain
        info = await ref.get_silo_info()
        assert info["grain_count"] >= 1

        await silo.stop()

    async def test_idle_timeout(self) -> None:
        silo = make_silo()
        await silo.start_background()

        ref = silo.grain_factory.get_grain(MgmtCounterGrain, "c1")
        info = await ref.get_silo_info()
        assert info["idle_timeout"] == 900.0

        await silo.stop()


class TestSiloManagementViaGateway:
    async def test_get_info_via_gateway(self) -> None:
        from pyleans.client import ClusterClient

        silo = make_silo()
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"])
        await client.connect()

        ref = client.get_grain(MgmtCounterGrain, "c1")
        info = await ref.get_silo_info()
        assert info["silo_id"] == silo._silo_id
        assert info["grain_count"] >= 1

        await client.close()
        await silo.stop()
