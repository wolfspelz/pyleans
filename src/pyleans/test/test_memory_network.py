"""Tests for InMemoryNetwork — the test-only in-process TCP simulator.

Every test runs with zero OS-level ports bound. These tests exercise the
simulator's fidelity against the full asyncio surface pyleans depends on:
round-trip I/O, backpressure, lifecycle, and failure injection.
"""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path

import pytest
from pyleans.net import InMemoryNetwork, InMemoryServer, MemoryStreamWriter, NetworkServer


class TestRoundTrip:
    """Basic connect + read + write behavior matches the real stack."""

    async def test_bytes_round_trip_in_order(self) -> None:
        received: list[bytes] = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.readexactly(11)
            received.append(data)
            writer.write(b"ack:" + data)
            await writer.drain()
            writer.close()

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 0)
        port = server.sockets[0].getsockname()[1]

        reader, writer = await net.open_connection("localhost", port)
        writer.write(b"hello ")
        writer.write(b"world")
        await writer.drain()

        response = await reader.readexactly(15)
        assert response == b"ack:hello world"
        assert received == [b"hello world"]

        writer.close()
        server.close()
        await server.wait_closed()

    async def test_shared_instance_routes_between_two_consumers(self) -> None:
        """Canonical fixture pattern: one InMemoryNetwork powers both ends."""
        net = InMemoryNetwork()

        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.readexactly(5)
            writer.write(data[::-1])
            await writer.drain()
            writer.close()

        server = await net.start_server(echo, "localhost", 0)
        port = server.sockets[0].getsockname()[1]

        reader, writer = await net.open_connection("localhost", port)
        writer.write(b"abcde")
        await writer.drain()

        assert await reader.readexactly(5) == b"edcba"

        writer.close()
        server.close()
        await server.wait_closed()


class TestConnectionRefused:
    async def test_open_connection_to_unbound_address_raises(self) -> None:
        net = InMemoryNetwork()
        with pytest.raises(ConnectionRefusedError, match="no server"):
            await net.open_connection("localhost", 40123)

    async def test_open_connection_after_server_close_raises(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()

        with pytest.raises(ConnectionRefusedError):
            await net.open_connection("localhost", port)


class TestAddressInUse:
    async def test_duplicate_address_raises_eaddrinuse(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 55555)
        try:
            with pytest.raises(OSError) as excinfo:
                await net.start_server(handler, "localhost", 55555)
            assert excinfo.value.errno == errno.EADDRINUSE
        finally:
            server.close()
            await server.wait_closed()

    async def test_same_address_after_close_is_reusable(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 55556)
        server.close()
        await server.wait_closed()

        server2 = await net.start_server(handler, "localhost", 55556)
        server2.close()
        await server2.wait_closed()


class TestVirtualPortAllocation:
    async def test_port_zero_assigns_monotonic_virtual_ports_above_40000(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = InMemoryNetwork()
        servers: list[NetworkServer] = []
        try:
            for _ in range(3):
                srv = await net.start_server(handler, "localhost", 0)
                servers.append(srv)
            ports = [s.sockets[0].getsockname()[1] for s in servers]
        finally:
            for s in servers:
                s.close()
                await s.wait_closed()

        assert all(p >= 40000 for p in ports)
        assert ports == sorted(ports)
        assert len(set(ports)) == 3

    async def test_per_instance_port_counters_independent(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net_a = InMemoryNetwork()
        net_b = InMemoryNetwork()
        srv_a = await net_a.start_server(handler, "localhost", 0)
        srv_b = await net_b.start_server(handler, "localhost", 0)
        try:
            assert srv_a.sockets[0].getsockname() == srv_b.sockets[0].getsockname()
        finally:
            srv_a.close()
            srv_b.close()
            await srv_a.wait_closed()
            await srv_b.wait_closed()


class TestSimulateReset:
    async def test_simulate_reset_raises_on_next_write(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            _reader, writer = await net.open_connection("localhost", port)
            assert isinstance(writer, MemoryStreamWriter)
            writer.simulate_reset()
            with pytest.raises(ConnectionResetError):
                writer.write(b"x")
            with pytest.raises(ConnectionResetError):
                await writer.drain()
        finally:
            server.close()
            await server.wait_closed()

    async def test_simulate_reset_unblocks_pending_drain(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            # Never consume from our reader; the client's drain() will block.
            await asyncio.sleep(10)

        net = InMemoryNetwork(high_water=64, low_water=16)
        server = await net.start_server(handler, "localhost", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            _reader, writer = await net.open_connection("localhost", port)
            assert isinstance(writer, MemoryStreamWriter)
            writer.write(b"x" * 200)  # cross high-water
            drain_task = asyncio.create_task(writer.drain())
            await asyncio.sleep(0.01)
            assert not drain_task.done()

            writer.simulate_reset()
            with pytest.raises(ConnectionResetError):
                await asyncio.wait_for(drain_task, timeout=1.0)
        finally:
            server.close()
            await server.wait_closed()


class TestSimulatePeerClose:
    async def test_simulate_peer_close_ends_reader(self) -> None:
        received_eof = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
            try:
                await reader.readexactly(10)
            except asyncio.IncompleteReadError:
                received_eof.set()

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            _reader, writer = await net.open_connection("localhost", port)
            assert isinstance(writer, MemoryStreamWriter)
            writer.write(b"partial")
            await writer.drain()
            writer.simulate_peer_close()
            await asyncio.wait_for(received_eof.wait(), timeout=1.0)
        finally:
            server.close()
            await server.wait_closed()


class TestDrainBackpressure:
    async def test_drain_blocks_above_high_water_releases_below_low_water(self) -> None:
        release = asyncio.Event()
        reader_holder: list[asyncio.StreamReader] = []

        async def handler(reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
            reader_holder.append(reader)
            await release.wait()

        net = InMemoryNetwork(high_water=64, low_water=16)
        server = await net.start_server(handler, "localhost", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            _client_reader, writer = await net.open_connection("localhost", port)

            # Push enough to cross high-water; drain should block.
            writer.write(b"0" * 100)
            drain_task = asyncio.create_task(writer.drain())
            await asyncio.sleep(0.01)
            assert not drain_task.done(), "drain should block above high-water"

            # Server reads enough to bring buffer down past low-water.
            assert reader_holder, "server handler should have received the reader"
            server_reader = reader_holder[0]
            _consumed = await server_reader.readexactly(90)
            await asyncio.wait_for(drain_task, timeout=1.0)
            assert drain_task.done()
            assert drain_task.exception() is None
        finally:
            release.set()
            server.close()
            await server.wait_closed()

    async def test_drain_returns_immediately_below_high_water(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            await asyncio.sleep(10)

        net = InMemoryNetwork(high_water=64 * 1024, low_water=16 * 1024)
        server = await net.start_server(handler, "localhost", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            _reader, writer = await net.open_connection("localhost", port)
            writer.write(b"small")
            await asyncio.wait_for(writer.drain(), timeout=0.1)
        finally:
            server.close()
            await server.wait_closed()


class TestServerLifecycle:
    async def test_close_stops_accepting(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 0)
        port = server.sockets[0].getsockname()[1]

        server.close()
        await server.wait_closed()

        with pytest.raises(ConnectionRefusedError):
            await net.open_connection("localhost", port)

    async def test_wait_closed_awaits_in_flight_handlers_without_cancelling(self) -> None:
        completed = asyncio.Event()
        ready = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
            ready.set()
            await reader.read()  # waits until peer closes
            await asyncio.sleep(0.02)
            completed.set()

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 0)
        port = server.sockets[0].getsockname()[1]

        _client_reader, client_writer = await net.open_connection("localhost", port)
        await ready.wait()

        # Close the server while a handler is active; the handler must finish naturally.
        server.close()
        client_writer.close()

        await asyncio.wait_for(server.wait_closed(), timeout=1.0)
        assert completed.is_set(), "handler must not be cancelled by close()"


class TestExtraInfo:
    async def test_peername_and_sockname_are_virtual_addresses(self) -> None:
        received: dict[str, object] = {}

        async def handler(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received["peername"] = writer.get_extra_info("peername")
            received["sockname"] = writer.get_extra_info("sockname")
            writer.close()

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await net.open_connection("localhost", port)
            assert writer.get_extra_info("sockname") == ("127.0.0.1", 40001)
            assert writer.get_extra_info("peername") == ("localhost", port)
            await reader.read()  # wait for handler to finish
            writer.close()
        finally:
            server.close()
            await server.wait_closed()

        assert received["sockname"] == ("localhost", port)
        assert received["peername"] == ("127.0.0.1", 40001)

    async def test_get_extra_info_returns_default_for_unknown_key(self) -> None:
        async def handler(_r: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            _reader, writer = await net.open_connection("localhost", port)
            assert writer.get_extra_info("nonsense", default="fallback") == "fallback"
            writer.close()
        finally:
            server.close()
            await server.wait_closed()


class TestSslParameter:
    async def test_ssl_is_accepted_and_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        import ssl

        async def handler(_r: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        ctx = ssl.create_default_context()
        net = InMemoryNetwork()
        with caplog.at_level("DEBUG", logger="pyleans.net.memory_network"):
            server = await net.start_server(handler, "localhost", 0, ssl=ctx)
            try:
                port = server.sockets[0].getsockname()[1]
                _reader, writer = await net.open_connection("localhost", port, ssl=ctx)
                writer.close()
            finally:
                server.close()
                await server.wait_closed()
        assert any("ssl parameter is ignored" in r.message for r in caplog.records)


class TestINetworkInterface:
    def test_inmemory_network_implements_inetwork(self) -> None:
        from pyleans.net import INetwork

        net = InMemoryNetwork()
        assert isinstance(net, INetwork)

    def test_re_exports_from_package(self) -> None:
        import pyleans as pyleans_root
        from pyleans import net

        assert net.InMemoryNetwork is InMemoryNetwork
        assert pyleans_root.InMemoryNetwork is InMemoryNetwork


class TestInMemoryServerType:
    async def test_server_is_network_server(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = InMemoryNetwork()
        server = await net.start_server(handler, "localhost", 0)
        try:
            assert isinstance(server, NetworkServer)
            assert isinstance(server, InMemoryServer)
        finally:
            server.close()
            await server.wait_closed()


class TestRegressionGuard:
    """Regression guard prevents test modules from using asyncio networking directly."""

    def test_scanner_detects_asyncio_start_server_call(self, tmp_path: Path) -> None:
        from conftest import scan_for_forbidden_asyncio_net_calls

        bad = tmp_path / "t_bad.py"
        bad.write_text("import asyncio\nasyncio.start_server(None, 'h', 0)\n")
        findings = scan_for_forbidden_asyncio_net_calls(bad)
        assert len(findings) == 1
        assert "start_server" in findings[0]

    def test_scanner_detects_asyncio_open_connection_call(self, tmp_path: Path) -> None:
        from conftest import scan_for_forbidden_asyncio_net_calls

        bad = tmp_path / "t_bad.py"
        bad.write_text(
            "import asyncio\nasync def f():\n    await asyncio.open_connection('h', 0)\n"
        )
        findings = scan_for_forbidden_asyncio_net_calls(bad)
        assert len(findings) == 1
        assert "open_connection" in findings[0]

    def test_scanner_detects_from_asyncio_import(self, tmp_path: Path) -> None:
        from conftest import scan_for_forbidden_asyncio_net_calls

        bad = tmp_path / "t_bad.py"
        bad.write_text("from asyncio import open_connection, start_server\n")
        findings = scan_for_forbidden_asyncio_net_calls(bad)
        assert len(findings) == 2
        assert any("open_connection" in f for f in findings)
        assert any("start_server" in f for f in findings)

    def test_scanner_ignores_correct_inetwork_usage(self, tmp_path: Path) -> None:
        from conftest import scan_for_forbidden_asyncio_net_calls

        good = tmp_path / "t_good.py"
        good.write_text(
            "from pyleans.net import InMemoryNetwork\n"
            "async def test_x():\n"
            "    net = InMemoryNetwork()\n"
            "    await net.start_server(None, 'h', 0)\n"
            "    await net.open_connection('h', 0)\n"
        )
        findings = scan_for_forbidden_asyncio_net_calls(good)
        assert findings == []

    def test_scanner_ignores_string_and_comment_occurrences(self, tmp_path: Path) -> None:
        from conftest import scan_for_forbidden_asyncio_net_calls

        mixed = tmp_path / "t_mixed.py"
        mixed.write_text(
            '"""Docstring mentioning asyncio.start_server and asyncio.open_connection."""\n'
            "# Comment about asyncio.start_server\n"
            "x = 'asyncio.open_connection'\n"
        )
        findings = scan_for_forbidden_asyncio_net_calls(mixed)
        assert findings == []

    def test_regression_guard_fails_collection_for_bad_test_module(self, tmp_path: Path) -> None:
        """End-to-end canary: pytest collection fails if a test uses asyncio networking directly."""
        # Copy the conftest into the temp test dir so the scanner hook runs.
        src_conftest = Path(__file__).parent / "conftest.py"
        (tmp_path / "conftest.py").write_text(src_conftest.read_text(encoding="utf-8"))
        # A test module that intentionally violates the rule:
        (tmp_path / "test_violator.py").write_text(
            "import asyncio\ndef test_bad():\n    asyncio.start_server(None, 'h', 0)\n"
        )
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "--no-header",
                "-o",
                "testpaths=.",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            check=False,
        )
        output = result.stdout + result.stderr
        assert result.returncode != 0, f"expected collection failure, got 0\n{output}"
        assert "INetwork" in output or "asyncio.start_server" in output, output
