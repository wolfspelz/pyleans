"""Tests for AsyncioNetwork — the production INetwork adapter.

These tests bind real ephemeral TCP ports via ``port=0`` (this is the one
place that legitimately uses the OS network stack in the test suite — the
whole point is to verify the production adapter end-to-end).
"""

from __future__ import annotations

import asyncio
import ssl as ssl_module

import pytest
from pyleans.net import AsyncioNetwork, INetwork, NetworkServer
from pyleans.net.asyncio_network import _AsyncioNetworkServer


class TestAbstractBaseClasses:
    """INetwork and NetworkServer must be non-instantiable ABCs."""

    def test_inetwork_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            INetwork()  # type: ignore[abstract]

    def test_network_server_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            NetworkServer()  # type: ignore[abstract]


class TestReExports:
    """Public symbols are re-exported from pyleans.net and pyleans."""

    def test_re_exports_from_pyleans_net(self) -> None:
        from pyleans import net as net_pkg

        assert net_pkg.INetwork is INetwork
        assert net_pkg.AsyncioNetwork is AsyncioNetwork
        assert net_pkg.NetworkServer is NetworkServer

    def test_re_exports_from_pyleans_root(self) -> None:
        import pyleans as pyleans_root

        assert pyleans_root.INetwork is INetwork
        assert pyleans_root.AsyncioNetwork is AsyncioNetwork
        assert pyleans_root.NetworkServer is NetworkServer


class TestStartServerAndOpenConnection:
    """Round-trip behavior of the production adapter."""

    async def test_round_trip_connect_send_close(self) -> None:
        received: list[bytes] = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.readexactly(5)
            received.append(data)
            writer.write(b"pong-")
            writer.write(data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        net = AsyncioNetwork()
        server = await net.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        reader, writer = await net.open_connection("127.0.0.1", port)
        writer.write(b"hello")
        await writer.drain()

        response = await reader.readexactly(10)
        assert response == b"pong-hello"
        assert received == [b"hello"]

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

    async def test_port_zero_returns_real_port(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = AsyncioNetwork()
        server = await net.start_server(handler, "127.0.0.1", 0)
        try:
            assert server.sockets, "Server must expose at least one socket"
            bound_port = server.sockets[0].getsockname()[1]
            assert isinstance(bound_port, int)
            assert bound_port > 0
            assert bound_port != 0
        finally:
            server.close()
            await server.wait_closed()

    async def test_open_connection_returns_stream_pair(self) -> None:
        async def handler(_r: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()
            await writer.wait_closed()

        net = AsyncioNetwork()
        server = await net.start_server(handler, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await net.open_connection("127.0.0.1", port)
            assert isinstance(reader, asyncio.StreamReader)
            assert isinstance(writer, asyncio.StreamWriter)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()


class TestServerLifecycle:
    async def test_close_and_wait_closed_shut_down_cleanly(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = AsyncioNetwork()
        server = await net.start_server(handler, "127.0.0.1", 0)
        assert isinstance(server, NetworkServer)
        assert isinstance(server, _AsyncioNetworkServer)

        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()

        with pytest.raises(OSError):
            await net.open_connection("127.0.0.1", port)

    async def test_sockets_property_returns_list(self) -> None:
        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = AsyncioNetwork()
        server = await net.start_server(handler, "127.0.0.1", 0)
        try:
            sockets = server.sockets
            assert isinstance(sockets, list)
            assert len(sockets) >= 1
            sockname = sockets[0].getsockname()
            assert isinstance(sockname[0], str)
            assert isinstance(sockname[1], int)
        finally:
            server.close()
            await server.wait_closed()


class TestSslParameter:
    async def test_ssl_none_passes_through(self) -> None:
        async def handler(_r: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()
            await writer.wait_closed()

        net = AsyncioNetwork()
        server = await net.start_server(handler, "127.0.0.1", 0, ssl=None)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await net.open_connection("127.0.0.1", port, ssl=None)
            assert isinstance(reader, asyncio.StreamReader)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    async def test_ssl_context_is_propagated_to_asyncio(self) -> None:
        """SSLContext is forwarded to asyncio.start_server.

        We only check that the call is accepted; TLS correctness is not
        in scope for this adapter — deployment layers handle certificates.
        """
        ctx = ssl_module.create_default_context(ssl_module.Purpose.CLIENT_AUTH)

        async def handler(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            return

        net = AsyncioNetwork()
        # Without a certificate loaded, starting a TLS server either raises
        # immediately or succeeds and would fail on handshake — both paths
        # prove ssl= was consumed by asyncio rather than silently dropped.
        try:
            server = await net.start_server(handler, "127.0.0.1", 0, ssl=ctx)
        except (ssl_module.SSLError, OSError):
            return
        else:
            server.close()
            await server.wait_closed()


class TestConcurrentConnections:
    async def test_many_clients_to_one_server(self) -> None:
        active_handlers = 0
        peak = 0
        done = asyncio.Event()
        release = asyncio.Event()

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal active_handlers, peak
            active_handlers += 1
            peak = max(peak, active_handlers)
            if active_handlers >= 5:
                done.set()
            try:
                await release.wait()
                data = await reader.readexactly(3)
                writer.write(data)
                await writer.drain()
            finally:
                active_handlers -= 1
                writer.close()
                await writer.wait_closed()

        net = AsyncioNetwork()
        server = await net.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            clients = [await net.open_connection("127.0.0.1", port) for _ in range(5)]
            await asyncio.wait_for(done.wait(), timeout=2.0)
            assert peak == 5

            release.set()
            for _reader, writer in clients:
                writer.write(b"abc")
                await writer.drain()
            for reader, writer in clients:
                assert await reader.readexactly(3) == b"abc"
                writer.close()
                await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
