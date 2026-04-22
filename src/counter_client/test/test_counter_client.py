"""Tests for the counter-client CLI."""

from typing import Any

import pytest
from pyleans.client import ClusterClient
from pyleans.grain import _grain_registry
from pyleans.net import InMemoryNetwork
from pyleans.providers.storage import StorageProvider
from pyleans.server.providers.memory_stream import InMemoryStreamProvider
from pyleans.server.silo import Silo
from pyleans.testing import InMemoryMembershipProvider

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


FakeMembershipProvider = InMemoryMembershipProvider


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
        command: str | None,
        counter_id: str | None,
        gateway: list[str] | str | None,
        value: int | None = None,
        membership: str = "./data/membership.md",
        cluster_id: str = "dev",
    ) -> None:
        self.command = command
        self.counter_id = counter_id
        self.gateway: list[str] | None
        if isinstance(gateway, str):
            self.gateway = [gateway]
        else:
            self.gateway = gateway
        self.value = value
        self.membership = membership
        self.cluster_id = cluster_id


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

    async def test_run_reload(
        self, network: InMemoryNetwork, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Arrange - set up a grain whose in-memory state diverges from storage
        storage = FakeStorageProvider()
        silo = Silo(
            grains=[CounterGrain],
            storage_providers={"default": storage},
            membership_provider=FakeMembershipProvider(),
            stream_providers={"default": InMemoryStreamProvider()},
            gateway_port=0,
            network=network,
        )
        await silo.start_background()
        client = ClusterClient(gateways=[f"localhost:{silo.gateway_port}"], network=network)
        await client.connect()
        counter = client.get_grain(CounterGrain, "reload-cli")
        await counter.set_value(5)
        await storage.write("CounterGrain", "reload-cli", {"value": 99}, None)
        await client.close()

        # Act - CLI reload command
        args = _FakeArgs(
            command="reload",
            counter_id="reload-cli",
            gateway=f"localhost:{silo.gateway_port}",
        )
        await run(args, network=network)  # type: ignore[arg-type]

        # Assert
        captured = capsys.readouterr()
        assert "Counter 'reload-cli': 99" in captured.out

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


class TestMembershipFallback:
    """When --gateway is omitted, pick a random active silo from the membership table."""

    async def test_picks_gateway_from_membership_table(
        self,
        network: InMemoryNetwork,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Any,
    ) -> None:
        # Arrange - a silo backed by a real markdown membership file
        from pyleans.server.providers.markdown_table_membership import (
            MarkdownTableMembershipProvider,
        )

        membership_path = str(tmp_path / "membership.md")
        membership = MarkdownTableMembershipProvider(membership_path)
        # Explicit gateway_port so the membership row advertises a reachable address
        silo = Silo(
            grains=[CounterGrain],
            storage_providers={"default": FakeStorageProvider()},
            membership_provider=membership,
            stream_providers={"default": InMemoryStreamProvider()},
            gateway_port=40501,
            network=network,
        )
        await silo.start_background()

        # Act - CLI with only --membership / --cluster-id, no --gateway
        args = _FakeArgs(
            command="inc",
            counter_id="from-membership",
            gateway=None,
            membership=membership_path,
            cluster_id="dev",
        )
        await run(args, network=network)  # type: ignore[arg-type]

        # Assert - the client dispatched through the membership-advertised gateway
        captured = capsys.readouterr()
        assert "Counter 'from-membership': 1" in captured.out

        await silo.stop()

    async def test_no_active_silos_exits(
        self,
        network: InMemoryNetwork,
        tmp_path: Any,
    ) -> None:
        # Arrange - empty membership file (no silos have registered yet)
        membership_path = str(tmp_path / "empty.md")
        args = _FakeArgs(
            command="get",
            counter_id="no-silos",
            gateway=None,
            membership=membership_path,
            cluster_id="dev",
        )

        # Act / Assert
        with pytest.raises(SystemExit) as exc_info:
            await run(args, network=network)  # type: ignore[arg-type]
        assert exc_info.value.code == 1

    async def test_cluster_id_filter_skips_other_clusters(
        self,
        network: InMemoryNetwork,
        tmp_path: Any,
    ) -> None:
        # Arrange - silo announces cluster "prod"; client asks for "dev"
        from pyleans.server.providers.markdown_table_membership import (
            MarkdownTableMembershipProvider,
        )

        membership_path = str(tmp_path / "other-cluster.md")
        membership = MarkdownTableMembershipProvider(membership_path)
        from pyleans.cluster.identity import ClusterId

        silo = Silo(
            grains=[CounterGrain],
            storage_providers={"default": FakeStorageProvider()},
            membership_provider=membership,
            stream_providers={"default": InMemoryStreamProvider()},
            gateway_port=40502,
            network=network,
            cluster_id=ClusterId("prod"),
        )
        await silo.start_background()

        args = _FakeArgs(
            command="get",
            counter_id="wrong-cluster",
            gateway=None,
            membership=membership_path,
            cluster_id="dev",
        )

        # Act / Assert - no eligible silo in "dev", client exits
        with pytest.raises(SystemExit) as exc_info:
            await run(args, network=network)  # type: ignore[arg-type]
        assert exc_info.value.code == 1

        await silo.stop()


class TestInteractiveMode:
    """Interactive mode — run() enters a REPL when args.command is None."""

    async def test_runs_multiple_commands_until_quit(
        self,
        network: InMemoryNetwork,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange - prepared transcript fed to the REPL's line reader
        from src.counter_client import main as cli

        silo = make_silo(network)
        await silo.start_background()

        transcript = iter(
            [
                "inc c1",
                "inc c1",
                "get c1",
                "set c1 10",
                "get c1",
                "quit",
            ]
        )

        async def fake_read_line(prompt: str) -> str | None:
            del prompt
            return next(transcript, None)

        monkeypatch.setattr(cli, "_read_line", fake_read_line)

        # Act
        args = _FakeArgs(
            command=None,
            counter_id=None,
            gateway=f"localhost:{silo.gateway_port}",
        )
        await run(args, network=network)  # type: ignore[arg-type]

        # Assert - each response line is in stdout
        captured = capsys.readouterr().out
        assert "Counter 'c1': 1" in captured
        assert "Counter 'c1': 2" in captured
        assert "Counter 'c1': 10" in captured

        await silo.stop()

    async def test_eof_exits_cleanly(
        self,
        network: InMemoryNetwork,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange - first line, then EOF
        from src.counter_client import main as cli

        silo = make_silo(network)
        await silo.start_background()

        transcript = iter(["inc foo", None])  # None models EOF

        async def fake_read_line(prompt: str) -> str | None:
            del prompt
            return next(transcript, None)

        monkeypatch.setattr(cli, "_read_line", fake_read_line)

        # Act - should return without raising
        args = _FakeArgs(
            command=None,
            counter_id=None,
            gateway=f"localhost:{silo.gateway_port}",
        )
        await run(args, network=network)  # type: ignore[arg-type]

        await silo.stop()

    async def test_status_command_prints_connected_gateway(
        self,
        network: InMemoryNetwork,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange
        from src.counter_client import main as cli

        silo = make_silo(network)
        await silo.start_background()
        gateway = f"localhost:{silo.gateway_port}"

        transcript = iter(["status", "quit"])

        async def fake_read_line(prompt: str) -> str | None:
            del prompt
            return next(transcript, None)

        monkeypatch.setattr(cli, "_read_line", fake_read_line)

        # Act
        args = _FakeArgs(command=None, counter_id=None, gateway=gateway)
        await run(args, network=network)  # type: ignore[arg-type]

        # Assert
        captured = capsys.readouterr().out
        assert f"connected_gateway: {gateway}" in captured

        await silo.stop()

    async def test_unknown_command_does_not_exit(
        self,
        network: InMemoryNetwork,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange
        from src.counter_client import main as cli

        silo = make_silo(network)
        await silo.start_background()

        transcript = iter(["bogus arg", "inc c1", "quit"])

        async def fake_read_line(prompt: str) -> str | None:
            del prompt
            return next(transcript, None)

        monkeypatch.setattr(cli, "_read_line", fake_read_line)

        # Act
        args = _FakeArgs(
            command=None,
            counter_id=None,
            gateway=f"localhost:{silo.gateway_port}",
        )
        await run(args, network=network)  # type: ignore[arg-type]

        # Assert - unknown command logged an error, but inc c1 still ran
        captured = capsys.readouterr()
        assert "unknown command" in captured.err
        assert "Counter 'c1': 1" in captured.out

        await silo.stop()

    async def test_missing_counter_id_prints_error(
        self,
        network: InMemoryNetwork,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange
        from src.counter_client import main as cli

        silo = make_silo(network)
        await silo.start_background()

        transcript = iter(["get", "quit"])

        async def fake_read_line(prompt: str) -> str | None:
            del prompt
            return next(transcript, None)

        monkeypatch.setattr(cli, "_read_line", fake_read_line)

        # Act
        args = _FakeArgs(
            command=None,
            counter_id=None,
            gateway=f"localhost:{silo.gateway_port}",
        )
        await run(args, network=network)  # type: ignore[arg-type]

        # Assert - error is printed on stderr; loop did not exit prematurely
        captured = capsys.readouterr()
        assert "'get' requires a counter_id" in captured.err

        await silo.stop()
