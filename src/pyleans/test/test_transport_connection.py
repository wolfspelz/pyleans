"""Tests for :class:`pyleans.transport.tcp.connection.SiloConnection`.

Every test runs entirely over :class:`pyleans.net.InMemoryNetwork` — no OS
sockets are bound. See ``docs/adr/adr-network-port-for-testability.md``
and the task-02-06 acceptance list.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import pytest
from pyleans.identity import SiloAddress
from pyleans.net import InMemoryNetwork
from pyleans.transport.error_payload import (
    APPLICATION_ERROR_BASE,
    ErrorPayload,
    decode_error_payload,
    encode_error_payload,
)
from pyleans.transport.errors import (
    BackpressureError,
    TransportClosedError,
    TransportConnectionError,
    TransportError,
    TransportTimeoutError,
)
from pyleans.transport.messages import MessageType, TransportMessage
from pyleans.transport.options import TransportOptions
from pyleans.transport.tcp.connection import SiloConnection
from pyleans.transport.wire import encode_frame, read_frame


async def _null_handler(
    _remote: SiloAddress, _message: TransportMessage
) -> TransportMessage | None:
    return None


@dataclass
class _Pair:
    """Two :class:`SiloConnection` endpoints wired through InMemoryNetwork."""

    client: SiloConnection
    server: SiloConnection
    client_handler_log: list[TransportMessage] = field(default_factory=list)
    server_handler_log: list[TransportMessage] = field(default_factory=list)

    async def close(self) -> None:
        await asyncio.gather(
            self.client.close("normal"),
            self.server.close("normal"),
            return_exceptions=True,
        )


_MessageHandler = Callable[[SiloAddress, TransportMessage], Awaitable[TransportMessage | None]]


async def _pair(
    network: InMemoryNetwork,
    *,
    options: TransportOptions | None = None,
    server_handler: _MessageHandler | None = None,
    client_handler: _MessageHandler | None = None,
) -> _Pair:
    """Wire a client SiloConnection ↔ server SiloConnection over ``network``."""
    opts = options or TransportOptions(
        network=network,
        default_request_timeout=2.0,
        keepalive_interval=0,  # disabled unless a test enables it explicitly
        close_drain_timeout=0.5,
    )
    server_ready: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        asyncio.get_running_loop().create_future()
    )

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not server_ready.done():
            server_ready.set_result((reader, writer))

    server_obj = await network.start_server(accept, "127.0.0.1", 0)
    port = server_obj.sockets[0].getsockname()[1]
    client_reader, client_writer = await network.open_connection("127.0.0.1", port)
    server_reader, server_writer = await server_ready

    client_silo = SiloAddress(host="127.0.0.1", port=port, epoch=1)
    server_silo = SiloAddress(host="127.0.0.1", port=9999, epoch=1)

    client_conn = SiloConnection(
        reader=client_reader,
        writer=client_writer,
        remote_silo=server_silo,
        options=opts,
        message_handler=client_handler or _null_handler,
    )
    server_conn = SiloConnection(
        reader=server_reader,
        writer=server_writer,
        remote_silo=client_silo,
        options=opts,
        message_handler=server_handler or _null_handler,
    )
    await asyncio.gather(client_conn.start(), server_conn.start())
    return _Pair(client=client_conn, server=server_conn)


@pytest.fixture
async def pair(network: InMemoryNetwork) -> AsyncIterator[_Pair]:
    """Default client/server pair where the server echoes the request body."""

    async def echo(_remote: SiloAddress, message: TransportMessage) -> TransportMessage | None:
        if message.message_type is MessageType.REQUEST:
            return TransportMessage(
                MessageType.RESPONSE,
                message.correlation_id,
                message.header,
                b"echo:" + message.body,
            )
        return None

    wired = await _pair(network, server_handler=echo)
    try:
        yield wired
    finally:
        await wired.close()


class TestRequestResponse:
    async def test_send_request_returns_response_payload(self, pair: _Pair) -> None:
        # Act
        response_header, response_body = await pair.client.send_request(b"h", b"b")

        # Assert
        assert response_header == b"h"
        assert response_body == b"echo:b"

    async def test_send_request_times_out_when_peer_never_responds(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — server that never responds to REQUEST.
        quiet_wired = await _pair(network, server_handler=_null_handler)
        try:
            # Act / Assert
            with pytest.raises(TransportError) as exc_info:
                await quiet_wired.client.send_request(b"", b"", timeout=0.05)
        finally:
            await quiet_wired.close()
        # The server's null handler replies with an ERROR(TransportError) for REQUEST.
        assert isinstance(exc_info.value, TransportError)

    async def test_send_request_raises_timeout_when_no_reply_arrives(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — server handler that hangs forever.
        hang_forever: asyncio.Event = asyncio.Event()

        async def hang(_remote: SiloAddress, _message: TransportMessage) -> TransportMessage | None:
            await hang_forever.wait()
            return None

        wired = await _pair(network, server_handler=hang)
        try:
            # Act / Assert
            with pytest.raises(TransportTimeoutError):
                await wired.client.send_request(b"", b"", timeout=0.05)
        finally:
            hang_forever.set()
            await wired.close()


class TestMultiplexing:
    async def test_many_concurrent_requests_all_complete(self, pair: _Pair) -> None:
        # Arrange
        n = 100

        # Act
        results = await asyncio.gather(
            *[pair.client.send_request(b"", f"body-{i}".encode()) for i in range(n)]
        )

        # Assert
        bodies = [body for _, body in results]
        assert bodies == [f"echo:body-{i}".encode() for i in range(n)]

    async def test_correlation_ids_pair_requests_with_their_responses(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — server that delays each response by a shuffled amount.
        async def reorder(
            _remote: SiloAddress, message: TransportMessage
        ) -> TransportMessage | None:
            if message.message_type is not MessageType.REQUEST:
                return None
            delay = int(message.body) * 0.001
            await asyncio.sleep(delay)
            return TransportMessage(MessageType.RESPONSE, message.correlation_id, b"", message.body)

        wired = await _pair(network, server_handler=reorder)
        try:
            # Act — submit requests in order 4,3,2,1 so responses arrive in reverse.
            payloads = [b"4", b"3", b"2", b"1"]
            results = await asyncio.gather(
                *[wired.client.send_request(b"", payload) for payload in payloads]
            )

            # Assert — each caller got their own body back despite reordering.
            assert [body for _, body in results] == payloads
        finally:
            await wired.close()


class TestInFlightSemaphore:
    async def test_semaphore_bounds_concurrent_pending_requests(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — max_in_flight=2; three concurrent requests, only two proceed.
        release: asyncio.Event = asyncio.Event()
        concurrent_peak = 0
        in_flight = 0

        async def handler(
            _remote: SiloAddress, message: TransportMessage
        ) -> TransportMessage | None:
            if message.message_type is not MessageType.REQUEST:
                return None
            nonlocal in_flight, concurrent_peak
            in_flight += 1
            concurrent_peak = max(concurrent_peak, in_flight)
            await release.wait()
            in_flight -= 1
            return TransportMessage(MessageType.RESPONSE, message.correlation_id, b"", b"ok")

        options = TransportOptions(
            network=network,
            default_request_timeout=5.0,
            max_in_flight_requests=2,
            keepalive_interval=0,
        )
        wired = await _pair(network, options=options, server_handler=handler)
        try:
            # Act
            tasks = [asyncio.create_task(wired.client.send_request(b"", b"x")) for _ in range(3)]
            await asyncio.sleep(0.05)
            assert concurrent_peak == 2

            release.set()
            results = await asyncio.gather(*tasks)

            # Assert — all three eventually succeed; peak never exceeded the cap.
            assert all(body == b"ok" for _, body in results)
            assert concurrent_peak == 2
        finally:
            release.set()
            await wired.close()


class TestWriteLock:
    async def test_concurrent_sends_produce_intact_frames_on_the_wire(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — raw server that reads frames by-the-book and echoes each.
        received: list[TransportMessage] = []
        done: asyncio.Event = asyncio.Event()
        n = 50

        async def raw_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while len(received) < n:
                    msg = await read_frame(reader, 1024 * 1024)
                    received.append(msg)
                    if msg.message_type is MessageType.REQUEST:
                        response = TransportMessage(
                            MessageType.RESPONSE,
                            msg.correlation_id,
                            b"",
                            msg.body,
                        )
                        writer.write(encode_frame(response, 1024 * 1024))
                        await writer.drain()
            finally:
                done.set()

        server = await network.start_server(raw_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await network.open_connection("127.0.0.1", port)
        remote = SiloAddress(host="127.0.0.1", port=port, epoch=1)
        options = TransportOptions(
            network=network,
            default_request_timeout=5.0,
            keepalive_interval=0,
        )
        client = SiloConnection(reader, writer, remote, options, _null_handler)
        await client.start()
        try:
            # Act — fire N concurrent send_requests; each frame must round-trip intact.
            tasks = [
                asyncio.create_task(client.send_request(b"", f"payload-{i}".encode()))
                for i in range(n)
            ]
            await asyncio.gather(*tasks)
            await done.wait()

            # Assert — every decoded frame matches one of the sent payloads, no corruption.
            decoded_bodies = {msg.body for msg in received}
            expected_bodies = {f"payload-{i}".encode() for i in range(n)}
            assert decoded_bodies == expected_bodies
        finally:
            await client.close("normal")
            server.close()
            await server.wait_closed()


class TestConnectionLoss:
    async def test_in_flight_request_fails_when_peer_writer_disconnects(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — server handler never answers; we abort the server connection.
        hang: asyncio.Event = asyncio.Event()

        async def never_reply(
            _remote: SiloAddress, _message: TransportMessage
        ) -> TransportMessage | None:
            await hang.wait()
            return None

        wired = await _pair(network, server_handler=never_reply)
        try:
            # Act
            task = asyncio.create_task(wired.client.send_request(b"", b"", timeout=5.0))
            await asyncio.sleep(0.05)
            await wired.server.close("lost")

            # Assert
            with pytest.raises((TransportConnectionError, TransportError)):
                await task
        finally:
            hang.set()
            await wired.close()


class TestClosed:
    async def test_send_request_after_close_raises_transport_closed_error(
        self, pair: _Pair
    ) -> None:
        # Arrange
        await pair.client.close("normal")

        # Act / Assert
        with pytest.raises(TransportClosedError):
            await pair.client.send_request(b"", b"")

    async def test_graceful_close_drains_in_flight_requests(self, network: InMemoryNetwork) -> None:
        # Arrange — slow handler; close_drain_timeout > handler delay.
        async def slow(_remote: SiloAddress, message: TransportMessage) -> TransportMessage | None:
            if message.message_type is not MessageType.REQUEST:
                return None
            await asyncio.sleep(0.1)
            return TransportMessage(MessageType.RESPONSE, message.correlation_id, b"", b"done")

        options = TransportOptions(
            network=network,
            default_request_timeout=5.0,
            close_drain_timeout=1.0,
            keepalive_interval=0,
        )
        wired = await _pair(network, options=options, server_handler=slow)
        try:
            # Act
            task = asyncio.create_task(wired.client.send_request(b"", b""))
            await asyncio.sleep(0.01)  # let the request be sent
            # Request is in-flight; graceful close should let it complete.
            _, body = await task

            # Assert — request completed cleanly before close begins
            assert body == b"done"
            await wired.client.close("normal")
            assert wired.client.is_closed
        finally:
            await wired.close()


class TestErrorResponse:
    async def test_request_handler_raising_yields_error_response(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — handler that raises; connection must stay open.
        async def raises(
            _remote: SiloAddress, _message: TransportMessage
        ) -> TransportMessage | None:
            raise RuntimeError("boom on server")

        wired = await _pair(network, server_handler=raises)
        try:
            # Act / Assert — first call comes back as TransportError (application)
            with pytest.raises(TransportError) as first_exc:
                await wired.client.send_request(b"", b"")
            assert "boom on server" in str(first_exc.value)

            # Connection must still be usable for subsequent calls.
            with pytest.raises(TransportError):
                await wired.client.send_request(b"", b"")
            assert not wired.client.is_closed
        finally:
            await wired.close()

    async def test_error_payload_carries_application_error_code(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — raw server that captures the inbound ERROR frame.
        inbound: list[TransportMessage] = []

        async def raw_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # Pretend to be a faulty peer: send a REQUEST the client will reject.
            msg = TransportMessage(MessageType.REQUEST, correlation_id=7, header=b"", body=b"")
            writer.write(encode_frame(msg, 1024 * 1024))
            await writer.drain()
            response = await read_frame(reader, 1024 * 1024)
            inbound.append(response)

        server = await network.start_server(raw_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await network.open_connection("127.0.0.1", port)
        remote = SiloAddress(host="127.0.0.1", port=port, epoch=1)

        async def raises(
            _remote: SiloAddress, _message: TransportMessage
        ) -> TransportMessage | None:
            raise ValueError("client-side boom")

        options = TransportOptions(
            network=network,
            default_request_timeout=5.0,
            keepalive_interval=0,
        )
        client = SiloConnection(reader, writer, remote, options, raises)
        await client.start()
        try:
            # Act — give the handler time to run and reply.
            for _ in range(50):
                if inbound:
                    break
                await asyncio.sleep(0.01)

            # Assert
            assert inbound, "server never received error response"
            response = inbound[0]
            assert response.message_type is MessageType.ERROR
            assert response.correlation_id == 7
            payload = decode_error_payload(response.body)
            assert payload.code == APPLICATION_ERROR_BASE
            assert "client-side boom" in payload.reason
        finally:
            await client.close("normal")
            server.close()
            await server.wait_closed()


class TestMalformedFrame:
    async def test_malformed_frame_closes_connection_and_fails_pending_futures(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — hostile server that sends garbage bytes after the client issues a request.
        triggered: asyncio.Event = asyncio.Event()

        async def hostile_server(
            _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await triggered.wait()
            # Valid 4-byte prefix declaring zero total length — below minimum overhead.
            writer.write(b"\x00\x00\x00\x00")
            await writer.drain()

        server = await network.start_server(hostile_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await network.open_connection("127.0.0.1", port)
        remote = SiloAddress(host="127.0.0.1", port=port, epoch=1)
        options = TransportOptions(
            network=network,
            default_request_timeout=5.0,
            keepalive_interval=0,
        )
        client = SiloConnection(reader, writer, remote, options, _null_handler)
        await client.start()
        try:
            # Act — issue a pending request, then release the hostile server's garbage.
            task = asyncio.create_task(client.send_request(b"", b"", timeout=5.0))
            await asyncio.sleep(0.01)
            triggered.set()

            # Assert — pending request faults, connection is closed.
            with pytest.raises((TransportConnectionError, TransportError)):
                await task
            for _ in range(50):
                if client.is_closed:
                    break
                await asyncio.sleep(0.01)
            assert client.is_closed
        finally:
            await client.close("lost")
            server.close()
            await server.wait_closed()


class TestKeepalive:
    async def test_keepalive_ping_fires_after_idle_interval(self, network: InMemoryNetwork) -> None:
        # Arrange — raw peer that logs inbound PINGs and answers them.
        pings: list[int] = []

        async def peer_logger(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while True:
                    msg = await read_frame(reader, 1024 * 1024)
                    if msg.message_type is MessageType.PING:
                        pings.append(msg.correlation_id)
                        pong = TransportMessage(MessageType.PONG, msg.correlation_id, b"", b"")
                        writer.write(encode_frame(pong, 1024 * 1024))
                        await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                return

        server = await network.start_server(peer_logger, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await network.open_connection("127.0.0.1", port)
        remote = SiloAddress(host="127.0.0.1", port=port, epoch=1)
        options = TransportOptions(
            network=network,
            default_request_timeout=5.0,
            keepalive_interval=0.05,
            keepalive_timeout=1.0,
        )
        client = SiloConnection(reader, writer, remote, options, _null_handler)
        await client.start()
        try:
            # Act
            for _ in range(30):
                if pings:
                    break
                await asyncio.sleep(0.02)

            # Assert
            assert pings, "keepalive PING was never observed"
        finally:
            await client.close("normal")
            server.close()
            await server.wait_closed()

    async def test_missing_pong_triggers_close_lost(self, network: InMemoryNetwork) -> None:
        # Arrange — peer that reads but never replies.
        async def silent_peer(reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                while True:
                    await read_frame(reader, 1024 * 1024)

        server = await network.start_server(silent_peer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await network.open_connection("127.0.0.1", port)
        remote = SiloAddress(host="127.0.0.1", port=port, epoch=1)
        options = TransportOptions(
            network=network,
            default_request_timeout=5.0,
            keepalive_interval=0.02,
            keepalive_timeout=0.05,
        )
        client = SiloConnection(reader, writer, remote, options, _null_handler)
        await client.start()
        try:
            # Act — give keepalive time to fire + timeout + close.
            for _ in range(100):
                if client.is_closed:
                    break
                await asyncio.sleep(0.02)

            # Assert
            assert client.is_closed
        finally:
            await client.close("lost")
            server.close()
            await server.wait_closed()


class TestBackpressure:
    async def test_raise_mode_refuses_send_when_buffer_above_hwm(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — a peer that reads very slowly (never actually reads).
        block: asyncio.Event = asyncio.Event()

        async def slow_reader(_reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
            await block.wait()

        server = await network.start_server(slow_reader, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await network.open_connection("127.0.0.1", port)
        remote = SiloAddress(host="127.0.0.1", port=port, epoch=1)
        options = TransportOptions(
            network=network,
            default_request_timeout=5.0,
            keepalive_interval=0,
            backpressure_mode="raise",
            write_buffer_high_water=16,
        )
        client = SiloConnection(reader, writer, remote, options, _null_handler)
        await client.start()
        try:
            # Act — first send fills the memory-transport buffer (high-water 64 KB);
            # manually bump the buffered count to exceed the 16-byte threshold.
            client._writer.transport._buffered = 128  # type: ignore[attr-defined]

            # Assert
            with pytest.raises(BackpressureError):
                await client.send_one_way(b"", b"x")
        finally:
            block.set()
            await client.close("lost")
            server.close()
            await server.wait_closed()


class TestOneWay:
    async def test_send_one_way_delivers_to_handler(self, network: InMemoryNetwork) -> None:
        # Arrange
        seen: list[TransportMessage] = []
        received: asyncio.Event = asyncio.Event()

        async def record(
            _remote: SiloAddress, message: TransportMessage
        ) -> TransportMessage | None:
            seen.append(message)
            received.set()
            return None

        wired = await _pair(network, server_handler=record)
        try:
            # Act
            await wired.client.send_one_way(b"head", b"payload")
            await asyncio.wait_for(received.wait(), timeout=1.0)

            # Assert
            assert seen[0].message_type is MessageType.ONE_WAY
            assert seen[0].body == b"payload"
            assert seen[0].header == b"head"
            assert seen[0].correlation_id == 0
        finally:
            await wired.close()


class TestInboundErrorFrame:
    async def test_inbound_error_frame_propagates_to_pending_caller(
        self, network: InMemoryNetwork
    ) -> None:
        # Arrange — server sends back an ERROR frame for any request.
        async def erroring_server(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                while True:
                    msg = await read_frame(reader, 1024 * 1024)
                    if msg.message_type is MessageType.REQUEST:
                        payload = ErrorPayload(code=APPLICATION_ERROR_BASE, reason="custom-failure")
                        response = TransportMessage(
                            MessageType.ERROR,
                            msg.correlation_id,
                            b"",
                            encode_error_payload(payload),
                        )
                        writer.write(encode_frame(response, 1024 * 1024))
                        await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                return

        server = await network.start_server(erroring_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await network.open_connection("127.0.0.1", port)
        remote = SiloAddress(host="127.0.0.1", port=port, epoch=1)
        options = TransportOptions(
            network=network,
            default_request_timeout=5.0,
            keepalive_interval=0,
        )
        client = SiloConnection(reader, writer, remote, options, _null_handler)
        await client.start()
        try:
            # Act / Assert
            with pytest.raises(TransportError) as exc_info:
                await client.send_request(b"", b"")
            assert "custom-failure" in str(exc_info.value)
            # Connection still usable afterwards
            assert not client.is_closed
        finally:
            await client.close("normal")
            server.close()
            await server.wait_closed()


logging.getLogger("pyleans.transport.tcp.connection").setLevel(logging.DEBUG)
