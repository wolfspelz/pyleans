"""Tests for :class:`pyleans.transport.tcp.manager.SiloConnectionManager`.

All tests run over :class:`pyleans.net.InMemoryNetwork` — no OS sockets.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from pyleans.cluster import ClusterId
from pyleans.identity import SiloAddress
from pyleans.net import InMemoryNetwork, NetworkServer
from pyleans.transport.cluster import MessageHandler
from pyleans.transport.errors import HandshakeError, TransportConnectionError
from pyleans.transport.messages import MessageType, TransportMessage
from pyleans.transport.options import TransportOptions
from pyleans.transport.tcp.manager import SiloConnectionManager

logging.getLogger("pyleans.transport.tcp").setLevel(logging.DEBUG)


async def _null_handler(
    _remote: SiloAddress, _message: TransportMessage
) -> TransportMessage | None:
    return None


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
class _Endpoint:
    silo: SiloAddress
    manager: SiloConnectionManager
    server: NetworkServer


async def _endpoint(
    network: InMemoryNetwork,
    *,
    host: str,
    port: int,
    cluster_id: ClusterId,
    handler: MessageHandler = _echo_handler,
    options: TransportOptions | None = None,
) -> _Endpoint:
    silo = SiloAddress(host=host, port=port, epoch=1)
    opts = options or _options(network)
    manager = SiloConnectionManager(silo, cluster_id, opts, handler)
    server = await network.start_server(manager.accept_inbound, host, port)
    # Re-fetch the actual bound port (support port=0 in simulator)
    actual = server.sockets[0].getsockname()[1]
    if actual != port:
        # Rebuild silo with the actual port so peers can dial us back.
        manager._local_silo = SiloAddress(host=host, port=actual, epoch=1)
    final_silo = SiloAddress(host=host, port=actual, epoch=1)
    return _Endpoint(silo=final_silo, manager=manager, server=server)


async def _stop_endpoints(*eps: _Endpoint) -> None:
    for ep in eps:
        await ep.manager.stop()
    for ep in eps:
        ep.server.close()
        await ep.server.wait_closed()


class TestConnect:
    async def test_connect_establishes_a_live_connection(self, network: InMemoryNetwork) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)

        try:
            # Act
            conn = await a.manager.connect(b.silo)

            # Assert
            assert not conn.is_closed
            response = await conn.send_request(b"", b"payload")
            assert response[1] == b"echo:payload"
        finally:
            await _stop_endpoints(a, b)

    async def test_concurrent_connects_coalesce_to_one_connection(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        try:
            # Act
            connections = await asyncio.gather(*[a.manager.connect(b.silo) for _ in range(10)])

            # Assert — every caller received the same SiloConnection object.
            assert all(conn is connections[0] for conn in connections)
            assert len(a.manager.get_connected_silos()) == 1
        finally:
            await _stop_endpoints(a, b)


class TestHandshakeFailure:
    async def test_cluster_id_mismatch_raises_handshake_error(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=ClusterId("alpha"))
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=ClusterId("beta"))
        try:
            # Act / Assert
            with pytest.raises(HandshakeError):
                await a.manager.connect(b.silo)
            assert a.manager.get_connection(b.silo) is None
        finally:
            await _stop_endpoints(a, b)

    async def test_connect_to_nonexistent_silo_raises_transport_error(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=ClusterId("alpha"))
        try:
            # Act / Assert
            with pytest.raises(TransportConnectionError):
                await a.manager.connect(SiloAddress(host="127.0.0.1", port=59999, epoch=1))
        finally:
            await _stop_endpoints(a)


class TestDedup:
    async def test_simultaneous_dial_yields_exactly_one_connection(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — two managers, each will try to dial the other at once.
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        try:
            # Act
            await asyncio.gather(
                a.manager.connect(b.silo),
                b.manager.connect(a.silo),
            )
            # Let any installation-side dedup settle.
            await asyncio.sleep(0.05)

            # Assert — both sides hold exactly one live connection each.
            assert len(a.manager.get_connected_silos()) == 1
            assert len(b.manager.get_connected_silos()) == 1

            # Either side's one surviving connection must still be usable.
            survivor = a.manager.get_connection(b.silo)
            assert survivor is not None and not survivor.is_closed
            _, body = await survivor.send_request(b"", b"ping")
            assert body == b"echo:ping"
        finally:
            await _stop_endpoints(a, b)


class TestCallbacks:
    async def test_on_established_fires_after_connect(self, network: InMemoryNetwork) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        established: list[SiloAddress] = []

        async def record(silo: SiloAddress) -> None:
            established.append(silo)

        a.manager.on_connection_established(record)
        try:
            # Act
            await a.manager.connect(b.silo)
            await asyncio.sleep(0.05)

            # Assert
            assert established == [b.silo]
        finally:
            await _stop_endpoints(a, b)

    async def test_on_lost_fires_after_connection_drops(self, network: InMemoryNetwork) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        lost: list[tuple[SiloAddress, Exception | None]] = []

        async def record(silo: SiloAddress, exc: Exception | None) -> None:
            lost.append((silo, exc))

        a.manager.on_connection_lost(record)
        try:
            conn = await a.manager.connect(b.silo)

            # Act — forcibly drop on a's side.
            await conn.close("lost")
            await asyncio.sleep(0.1)

            # Assert
            assert lost
            assert lost[0][0] == b.silo
        finally:
            await _stop_endpoints(a, b)

    async def test_callback_exception_does_not_disrupt_manager(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)

        async def broken(_silo: SiloAddress) -> None:
            raise RuntimeError("bad observer")

        a.manager.on_connection_established(broken)
        try:
            # Act
            conn = await a.manager.connect(b.silo)
            await asyncio.sleep(0.01)

            # Assert — manager continues to serve despite the raising callback.
            assert not conn.is_closed
            _, body = await conn.send_request(b"", b"ok")
            assert body == b"echo:ok"
        finally:
            await _stop_endpoints(a, b)


class TestDisconnect:
    async def test_disconnect_closes_and_cancels_reconnect(self, network: InMemoryNetwork) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        try:
            conn = await a.manager.connect(b.silo)
            assert not conn.is_closed

            # Act
            await a.manager.disconnect(b.silo)

            # Assert
            assert a.manager.get_connection(b.silo) is None
        finally:
            await _stop_endpoints(a, b)


class TestReconnect:
    async def test_reconnect_loop_retries_until_peer_is_inactive(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)

        sleeps: list[float] = []

        async def instrumented_sleep(delay: float) -> None:
            sleeps.append(delay)
            await asyncio.sleep(0)

        # Swap the sleep function on an already-constructed manager.
        a.manager._sleep = instrumented_sleep
        try:
            # First connect succeeds.
            conn = await a.manager.connect(b.silo)
            # Mark b as active so the manager will keep reconnecting after loss.
            a.manager._active_silos.add(b.silo)
            # Act — drop the connection; reconnect loop should fire.
            await conn.close("lost")
            for _ in range(50):
                if sleeps:
                    break
                await asyncio.sleep(0.01)

            # Assert — at least one reconnect sleep was scheduled.
            assert sleeps, "reconnect loop never ran"
            # With jitter_fraction=0, first delay equals base_delay.
            assert sleeps[0] == pytest.approx(a.manager._options.reconnect_base_delay, abs=1e-9)
        finally:
            await _stop_endpoints(a, b)

    async def test_reconnect_stops_when_silo_leaves_active_set(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        try:
            conn = await a.manager.connect(b.silo)
            a.manager._active_silos.add(b.silo)
            await conn.close("lost")
            # Reconnect loop is now scheduled; drop b from active.
            await asyncio.sleep(0.02)

            # Act
            a.manager._active_silos.discard(b.silo)
            # Give the loop a chance to notice.
            await asyncio.sleep(0.1)

            # Assert — reconnect task eventually exits.
            reconnect = a.manager._reconnect_tasks.get(b.silo)
            # Either the task already exited (and was removed), or it's done.
            assert reconnect is None or reconnect.done()
        finally:
            await _stop_endpoints(a, b)


class TestUpdateActiveSilos:
    async def test_update_opens_new_and_closes_removed_peers(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        c = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        try:
            # Act — declare b and c active; eager connects should fire.
            await a.manager.update_active_silos({b.silo, c.silo})
            for _ in range(50):
                if (
                    a.manager.get_connection(b.silo) is not None
                    and a.manager.get_connection(c.silo) is not None
                ):
                    break
                await asyncio.sleep(0.01)

            assert a.manager.get_connection(b.silo) is not None
            assert a.manager.get_connection(c.silo) is not None

            # Drop c from active → its connection should be closed.
            await a.manager.update_active_silos({b.silo})
            await asyncio.sleep(0.05)

            # Assert
            assert a.manager.get_connection(b.silo) is not None
            assert a.manager.get_connection(c.silo) is None
        finally:
            await _stop_endpoints(a, b, c)


class TestStop:
    async def test_stop_closes_all_connections(self, network: InMemoryNetwork) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        b = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        c = await _endpoint(network, host="127.0.0.1", port=0, cluster_id=cluster)
        try:
            await a.manager.connect(b.silo)
            await a.manager.connect(c.silo)
            assert len(a.manager.get_connected_silos()) == 2

            # Act
            await a.manager.stop()

            # Assert
            assert a.manager.get_connected_silos() == []
        finally:
            a.server.close()
            await a.server.wait_closed()
            await _stop_endpoints(b, c)


class TestBackoffCurve:
    async def test_delay_grows_exponentially_up_to_max(self, network: InMemoryNetwork) -> None:
        # Arrange
        cluster = ClusterId("alpha")
        a_opts = _options(network)
        a_opts.reconnect_base_delay = 0.01
        a_opts.reconnect_max_delay = 0.08
        a_opts.reconnect_jitter_fraction = 0.0
        a = await _endpoint(
            network,
            host="127.0.0.1",
            port=0,
            cluster_id=cluster,
            options=a_opts,
        )
        try:
            sleeps: list[float] = []

            async def fake_sleep(delay: float) -> None:
                sleeps.append(delay)
                # Give control back so loop can iterate.
                await asyncio.sleep(0)

            a.manager._sleep = fake_sleep
            missing = SiloAddress(host="127.0.0.1", port=59998, epoch=1)
            a.manager._active_silos.add(missing)
            # Act — drive the reconnect loop against an unreachable peer.
            a.manager._schedule_reconnect(missing)
            for _ in range(100):
                if len(sleeps) >= 5:
                    break
                await asyncio.sleep(0.005)
            a.manager._active_silos.discard(missing)
            await asyncio.sleep(0.05)

            # Assert — first five delays: 0.01, 0.02, 0.04, 0.08, 0.08 (capped).
            assert sleeps[:5] == pytest.approx([0.01, 0.02, 0.04, 0.08, 0.08], abs=1e-9)
        finally:
            await _stop_endpoints(a)


class TestParse:
    async def test_silo_address_from_silo_id_roundtrip(self) -> None:
        # Arrange
        original = SiloAddress(host="10.0.0.5", port=11111, epoch=1713441000)

        # Act
        parsed = SiloAddress.from_silo_id(original.silo_id)

        # Assert
        assert parsed == original

    async def test_silo_address_from_silo_id_rejects_malformed(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            SiloAddress.from_silo_id("bad-input")


# Helpers can be used without mypy/pytest dependencies.
_Callback = Callable[[SiloAddress], Awaitable[None]]
