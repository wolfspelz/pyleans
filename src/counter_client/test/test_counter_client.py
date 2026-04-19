"""Tests for the counter-client CLI."""

from typing import Any

import pytest
from pyleans.client import ClusterClient
from pyleans.grain import _grain_registry
from pyleans.identity import SiloStatus
from pyleans.net import InMemoryNetwork
from pyleans.providers.membership import MembershipProvider
from pyleans.providers.storage import StorageProvider
from pyleans.server.providers.memory_stream import InMemoryStreamProvider
from pyleans.server.silo import Silo

from src.counter_app.counter_grain import CounterGrain
from src.counter_client.main import run

_UNBOUND_GATEWAY = "localhost:59999"

# --- Fake providers ---


class FakeStorageProvider(StorageProvider):
    def __init__(self) -> None:
        self._store: dict[str, tuple[dict[str, Any], str]] = {}

    async def read(self, grain_type: str, grain_key: str) -> tuple[dict[str, Any], str | None]:
        key = f"{grain_type}/{grain_key}"
        if key in self._store:
            state, etag = self._store[key]
            return state, etag
        return {}, None

    async def write(
        self,
        grain_type: str,
        grain_key: str,
        state: dict[str, Any],
        expected_etag: str | None,
    ) -> str:
        import time

        key = f"{grain_type}/{grain_key}"
        new_etag = str(time.monotonic())
        self._store[key] = (state, new_etag)
        return new_etag

    async def clear(
        self,
        grain_type: str,
        grain_key: str,
        expected_etag: str | None,
    ) -> None:
        key = f"{grain_type}/{grain_key}"
        self._store.pop(key, None)


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


# --- Fixtures ---


@pytest.fixture(autouse=True)
def _ensure_grain_registered() -> None:
    _grain_registry["CounterGrain"] = CounterGrain


def make_silo(network: InMemoryNetwork) -> Silo:
    return Silo(
        grains=[CounterGrain],
        storage_providers={"default": FakeStorageProvider()},
        membership_provider=FakeMembershipProvider(),
        stream_providers={"default": InMemoryStreamProvider()},
        gateway_port=0,
        network=network,
    )


class _FakeArgs:
    """Mimics argparse.Namespace for CLI tests."""

    def __init__(
        self,
        command: str,
        counter_id: str,
        gateway: str,
        value: int | None = None,
    ) -> None:
        self.command = command
        self.counter_id = counter_id
        self.gateway = gateway
        self.value = value


# --- Tests ---


class TestClientGetCommand:
    async def test_get_returns_zero_initially(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()
        counter = client.get_grain(CounterGrain, "test")
        assert await counter.get_value() == 0
        await client.close()
        await silo.stop()


class TestClientIncCommand:
    async def test_increment_returns_new_value(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()
        counter = client.get_grain(CounterGrain, "test")
        assert await counter.increment() == 1
        assert await counter.increment() == 2
        await client.close()
        await silo.stop()


class TestClientSetCommand:
    async def test_set_value(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()
        counter = client.get_grain(CounterGrain, "test")
        await counter.set_value(42)
        assert await counter.get_value() == 42
        await client.close()
        await silo.stop()


class TestClientConnectionError:
    async def test_connect_to_down_silo_raises(self, network: InMemoryNetwork) -> None:
        client = ClusterClient(gateways=[_UNBOUND_GATEWAY], network=network)
        with pytest.raises(ConnectionError):
            await client.connect()


class TestRunFunction:
    """Test the async run() function used by the CLI."""

    async def test_run_get(
        self, network: InMemoryNetwork, capsys: pytest.CaptureFixture[str]
    ) -> None:
        silo = make_silo(network)
        await silo.start_background()

        args = _FakeArgs(
            command="get",
            counter_id="foo",
            gateway=f"localhost:{silo.gateway_port}",
        )
        await run(args, network=network)  # type: ignore[arg-type]
        captured = capsys.readouterr()
        assert "Counter 'foo': 0" in captured.out

        await silo.stop()

    async def test_run_inc(
        self, network: InMemoryNetwork, capsys: pytest.CaptureFixture[str]
    ) -> None:
        silo = make_silo(network)
        await silo.start_background()

        args = _FakeArgs(
            command="inc",
            counter_id="bar",
            gateway=f"localhost:{silo.gateway_port}",
        )
        await run(args, network=network)  # type: ignore[arg-type]
        captured = capsys.readouterr()
        assert "Counter 'bar': 1" in captured.out

        await silo.stop()

    async def test_run_set(
        self, network: InMemoryNetwork, capsys: pytest.CaptureFixture[str]
    ) -> None:
        silo = make_silo(network)
        await silo.start_background()

        args = _FakeArgs(
            command="set",
            counter_id="baz",
            gateway=f"localhost:{silo.gateway_port}",
            value=100,
        )
        await run(args, network=network)  # type: ignore[arg-type]
        captured = capsys.readouterr()
        assert "Counter 'baz': 100" in captured.out

        await silo.stop()

    async def test_run_set_without_value_exits(self, network: InMemoryNetwork) -> None:
        silo = make_silo(network)
        await silo.start_background()

        args = _FakeArgs(
            command="set",
            counter_id="baz",
            gateway=f"localhost:{silo.gateway_port}",
            value=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            await run(args, network=network)  # type: ignore[arg-type]
        assert exc_info.value.code == 1

        await silo.stop()

    async def test_run_info(
        self, network: InMemoryNetwork, capsys: pytest.CaptureFixture[str]
    ) -> None:
        silo = make_silo(network)
        await silo.start_background()

        args = _FakeArgs(
            command="info",
            counter_id="any",
            gateway=f"localhost:{silo.gateway_port}",
        )
        await run(args, network=network)  # type: ignore[arg-type]
        captured = capsys.readouterr()
        assert "Silo info (via 'any'):" in captured.out
        assert "silo_id:" in captured.out
        assert "grain_count:" in captured.out

        await silo.stop()

    async def test_run_connection_error_exits(self, network: InMemoryNetwork) -> None:
        args = _FakeArgs(
            command="get",
            counter_id="foo",
            gateway=_UNBOUND_GATEWAY,
        )
        with pytest.raises(SystemExit) as exc_info:
            await run(args, network=network)  # type: ignore[arg-type]
        assert exc_info.value.code == 1
