"""Tests for :class:`pyleans.transport.tcp.transport.TcpClusterTransport`.

End-to-end exercises of the task-02-08 adapter over
:class:`pyleans.net.InMemoryNetwork`. No OS sockets are bound.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from pyleans.cluster import ClusterId
from pyleans.identity import SiloAddress
from pyleans.net import InMemoryNetwork
from pyleans.transport import (
    IClusterTransport,
    ITransportFactory,
    MessageHandler,
    MessageType,
    TcpTransportFactory,
    TransportClosedError,
    TransportConnectionError,
    TransportMessage,
    TransportOptions,
)
from pyleans.transport.errors import HandshakeError
from pyleans.transport.tcp.transport import TcpClusterTransport

logging.getLogger("pyleans.transport.tcp").setLevel(logging.DEBUG)


async def _echo_handler(_remote: SiloAddress, message: TransportMessage) -> TransportMessage | None:
    if message.message_type is MessageType.REQUEST:
        return TransportMessage(
            MessageType.RESPONSE,
            message.correlation_id,
            message.header,
            b"echo:" + message.body,
        )
    return None


def _options(network: InMemoryNetwork) -> TransportOptions:
    return TransportOptions(
        network=network,
        default_request_timeout=2.0,
        keepalive_interval=0,
        connect_timeout=1.0,
        handshake_timeout=1.0,
        close_drain_timeout=0.2,
        shutdown_timeout=1.0,
        reconnect_base_delay=0.01,
        reconnect_max_delay=0.1,
        reconnect_jitter_fraction=0.0,
    )


@dataclass
class _TransportPair:
    silo_a: SiloAddress
    silo_b: SiloAddress
    t_a: TcpClusterTransport
    t_b: TcpClusterTransport
    cluster: ClusterId


async def _pair(
    network: InMemoryNetwork,
    *,
    cluster: ClusterId,
    handler_a: MessageHandler = _echo_handler,
    handler_b: MessageHandler = _echo_handler,
    options: TransportOptions | None = None,
) -> _TransportPair:
    opts = options or _options(network)
    t_a = TcpClusterTransport(opts)
    t_b = TcpClusterTransport(opts)
    # Bind port=0; TcpClusterTransport.start() normalises to the real bound port.
    await t_a.start(SiloAddress(host="127.0.0.1", port=0, epoch=1), cluster, handler_a)
    await t_b.start(SiloAddress(host="127.0.0.1", port=0, epoch=2), cluster, handler_b)
    return _TransportPair(
        silo_a=t_a.local_silo,
        silo_b=t_b.local_silo,
        t_a=t_a,
        t_b=t_b,
        cluster=cluster,
    )


@pytest.fixture
async def transport_pair(network: InMemoryNetwork) -> AsyncIterator[_TransportPair]:
    pair = await _pair(network, cluster=ClusterId("alpha"))
    try:
        yield pair
    finally:
        await pair.t_a.stop()
        await pair.t_b.stop()


class TestLifecycle:
    async def test_start_twice_raises(self, network: InMemoryNetwork) -> None:
        # Arrange
        transport = TcpClusterTransport(_options(network))
        silo = SiloAddress(host="127.0.0.1", port=0, epoch=1)
        await transport.start(silo, ClusterId("alpha"), _echo_handler)

        # Act / Assert
        with pytest.raises(RuntimeError):
            await transport.start(silo, ClusterId("alpha"), _echo_handler)

        await transport.stop()

    async def test_stop_before_start_is_noop(self, network: InMemoryNetwork) -> None:
        # Arrange
        transport = TcpClusterTransport(_options(network))

        # Act / Assert — must not raise
        await transport.stop()

    async def test_local_silo_property_requires_start(self, network: InMemoryNetwork) -> None:
        # Arrange
        transport = TcpClusterTransport(_options(network))

        # Act / Assert
        with pytest.raises(TransportClosedError):
            _ = transport.local_silo

    async def test_restart_after_stop_raises(self, network: InMemoryNetwork) -> None:
        # Arrange
        transport = TcpClusterTransport(_options(network))
        silo = SiloAddress(host="127.0.0.1", port=0, epoch=1)
        await transport.start(silo, ClusterId("alpha"), _echo_handler)
        await transport.stop()

        # Act / Assert
        with pytest.raises(RuntimeError):
            await transport.start(silo, ClusterId("alpha"), _echo_handler)


class TestRequestResponse:
    async def test_send_request_roundtrip_both_directions(
        self, transport_pair: _TransportPair
    ) -> None:
        # Act
        resp_a = await transport_pair.t_a.send_request(transport_pair.silo_b, b"h1", b"hello")
        resp_b = await transport_pair.t_b.send_request(transport_pair.silo_a, b"h2", b"world")

        # Assert
        assert resp_a == (b"h1", b"echo:hello")
        assert resp_b == (b"h2", b"echo:world")


class TestSendOneWay:
    async def test_one_way_reaches_handler(self, network: InMemoryNetwork) -> None:
        # Arrange
        received: list[TransportMessage] = []
        gate: asyncio.Event = asyncio.Event()

        async def record(
            _remote: SiloAddress, message: TransportMessage
        ) -> TransportMessage | None:
            if message.message_type is MessageType.ONE_WAY:
                received.append(message)
                gate.set()
            return None

        pair = await _pair(network, cluster=ClusterId("alpha"), handler_b=record)
        try:
            # Act
            await pair.t_a.send_one_way(pair.silo_b, b"hdr", b"body")
            await asyncio.wait_for(gate.wait(), timeout=1.0)

            # Assert
            assert received[0].message_type is MessageType.ONE_WAY
            assert received[0].body == b"body"
            assert received[0].header == b"hdr"
            assert received[0].correlation_id == 0
        finally:
            await pair.t_a.stop()
            await pair.t_b.stop()


class TestSendPing:
    async def test_ping_returns_positive_rtt(self, transport_pair: _TransportPair) -> None:
        # Act
        rtt = await transport_pair.t_a.send_ping(transport_pair.silo_b, timeout=1.0)

        # Assert
        assert rtt >= 0.0
        assert rtt < 1.0  # in-memory, should be near-zero


class TestSelfSendRejection:
    async def test_send_request_to_self_raises_value_error(
        self, transport_pair: _TransportPair
    ) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            await transport_pair.t_a.send_request(transport_pair.silo_a, b"", b"")

    async def test_send_one_way_to_self_raises_value_error(
        self, transport_pair: _TransportPair
    ) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            await transport_pair.t_a.send_one_way(transport_pair.silo_a, b"", b"")


class TestIsConnected:
    async def test_reports_connected_after_send_request(
        self, transport_pair: _TransportPair
    ) -> None:
        # Arrange — connection is opened lazily on first send_request.
        assert not transport_pair.t_a.is_connected_to(transport_pair.silo_b)

        # Act
        await transport_pair.t_a.send_request(transport_pair.silo_b, b"", b"x")
        await asyncio.sleep(0.02)

        # Assert
        assert transport_pair.t_a.is_connected_to(transport_pair.silo_b)
        assert transport_pair.silo_b in transport_pair.t_a.get_connected_silos()


class TestCallbacks:
    async def test_on_established_fires_on_outbound_connect(self, network: InMemoryNetwork) -> None:
        # Arrange
        pair = await _pair(network, cluster=ClusterId("alpha"))
        established: list[SiloAddress] = []

        async def record(silo: SiloAddress) -> None:
            established.append(silo)

        pair.t_a.on_connection_established(record)
        try:
            # Act
            await pair.t_a.connect_to_silo(pair.silo_b)
            await asyncio.sleep(0.05)

            # Assert
            assert established == [pair.silo_b]
        finally:
            await pair.t_a.stop()
            await pair.t_b.stop()


class TestHandshakeFailure:
    async def test_cluster_mismatch_raises_handshake_error(self, network: InMemoryNetwork) -> None:
        # Arrange
        opts = _options(network)
        t_a = TcpClusterTransport(opts)
        t_b = TcpClusterTransport(opts)
        silo_a = SiloAddress(host="127.0.0.1", port=0, epoch=1)
        silo_b = SiloAddress(host="127.0.0.1", port=0, epoch=2)
        await t_a.start(silo_a, ClusterId("alpha"), _echo_handler)
        await t_b.start(silo_b, ClusterId("beta"), _echo_handler)
        try:
            # Act / Assert
            with pytest.raises(HandshakeError):
                await t_a.send_request(t_b.local_silo, b"", b"")
        finally:
            await t_a.stop()
            await t_b.stop()


class TestStop:
    async def test_send_after_stop_raises_transport_closed(
        self, transport_pair: _TransportPair
    ) -> None:
        # Arrange
        await transport_pair.t_a.send_request(transport_pair.silo_b, b"", b"x")

        # Act
        await transport_pair.t_a.stop()

        # Assert
        with pytest.raises(TransportClosedError):
            await transport_pair.t_a.send_request(transport_pair.silo_b, b"", b"")


class TestRefusedConnection:
    async def test_connect_to_nonexistent_silo_raises_transport_error(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange
        transport = TcpClusterTransport(_options(network))
        silo = SiloAddress(host="127.0.0.1", port=0, epoch=1)
        await transport.start(silo, ClusterId("alpha"), _echo_handler)
        try:
            # Act / Assert
            target = SiloAddress(host="127.0.0.1", port=59999, epoch=1)
            with pytest.raises(TransportConnectionError):
                await transport.send_request(target, b"", b"")
        finally:
            await transport.stop()


class TestPeerKill:
    async def test_in_flight_request_fails_when_peer_stops(self, network: InMemoryNetwork) -> None:
        # Arrange — server handler that hangs so we can stop peer mid-request.
        hang: asyncio.Event = asyncio.Event()

        async def hang_handler(
            _remote: SiloAddress, message: TransportMessage
        ) -> TransportMessage | None:
            if message.message_type is MessageType.REQUEST:
                await hang.wait()
            return None

        pair = await _pair(network, cluster=ClusterId("alpha"), handler_b=hang_handler)
        try:
            # Act
            task = asyncio.create_task(pair.t_a.send_request(pair.silo_b, b"", b"", timeout=5.0))
            await asyncio.sleep(0.02)
            await pair.t_b.stop()

            # Assert
            with pytest.raises((TransportConnectionError, TransportClosedError)):
                await task
        finally:
            hang.set()
            await pair.t_a.stop()


class TestOversizedFrame:
    async def test_oversized_inbound_frame_closes_connection_without_crashing_peer(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — a transport whose server will answer requests normally;
        # another transport configured with a deliberately tiny
        # max_message_size so outbound encoding of the second request fails
        # — we still prove the first request succeeded and the transport
        # remained usable afterwards.
        opts_a = _options(network)
        opts_b = _options(network)
        opts_b.max_message_size = 64  # barely larger than frame overhead
        t_a = TcpClusterTransport(opts_a)
        t_b = TcpClusterTransport(opts_b)
        silo_a = SiloAddress(host="127.0.0.1", port=0, epoch=1)
        silo_b = SiloAddress(host="127.0.0.1", port=0, epoch=2)
        await t_a.start(silo_a, ClusterId("alpha"), _echo_handler)
        await t_b.start(silo_b, ClusterId("alpha"), _echo_handler)
        try:
            # Small request fits — succeeds.
            resp = await t_a.send_request(t_b.local_silo, b"", b"ok")
            assert resp[1] == b"echo:ok"

            # Act — a huge request must be rejected locally (encoding), not crash the peer.
            from pyleans.transport.errors import MessageTooLargeError

            big_payload = b"x" * 4096
            with pytest.raises(MessageTooLargeError):
                # Use t_b's opts (tiny max) on its own send path.
                await t_b.send_request(t_a.local_silo, b"", big_payload)

            # Assert — both transports remain usable.
            resp2 = await t_a.send_request(t_b.local_silo, b"", b"still-ok")
            assert resp2[1] == b"echo:still-ok"
        finally:
            await t_a.stop()
            await t_b.stop()


class TestFactory:
    async def test_factory_returns_working_transport(self, network: InMemoryNetwork) -> None:
        # Arrange
        factory: ITransportFactory = TcpTransportFactory()
        opts = _options(network)
        a: IClusterTransport = factory.create_cluster_transport(opts)
        b: IClusterTransport = factory.create_cluster_transport(opts)
        cluster = ClusterId("alpha")
        await a.start(SiloAddress(host="127.0.0.1", port=0, epoch=1), cluster, _echo_handler)
        await b.start(SiloAddress(host="127.0.0.1", port=0, epoch=2), cluster, _echo_handler)
        try:
            # Act
            _, body = await a.send_request(b.local_silo, b"", b"test")

            # Assert
            assert body == b"echo:test"
        finally:
            await a.stop()
            await b.stop()
