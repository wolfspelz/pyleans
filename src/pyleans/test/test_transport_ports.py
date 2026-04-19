"""Tests for pyleans.transport — ABCs, message types, error hierarchy, options."""

import inspect

import pytest
from pyleans.net import AsyncioNetwork, InMemoryNetwork
from pyleans.transport import (
    BackpressureError,
    HandshakeError,
    IClusterTransport,
    ITransportFactory,
    MessageDirection,
    MessageTooLargeError,
    MessageType,
    TransportClosedError,
    TransportConnectionError,
    TransportError,
    TransportMessage,
    TransportOptions,
    TransportTimeoutError,
)


class TestMessageType:
    def test_wire_codepoints_match_architecture_doc(self) -> None:
        # Assert
        assert int(MessageType.REQUEST) == 0x01
        assert int(MessageType.RESPONSE) == 0x02
        assert int(MessageType.ONE_WAY) == 0x03
        assert int(MessageType.PING) == 0x04
        assert int(MessageType.PONG) == 0x05
        assert int(MessageType.ERROR) == 0x06

    def test_int_enum_usable_as_int(self) -> None:
        # Act
        result = MessageType.REQUEST + 0

        # Assert
        assert result == 0x01


class TestMessageDirection:
    def test_enum_has_both_directions(self) -> None:
        # Assert
        assert int(MessageDirection.OUTBOUND) == 1
        assert int(MessageDirection.INBOUND) == 2


class TestTransportMessage:
    def test_frozen_prevents_mutation(self) -> None:
        # Arrange
        msg = TransportMessage(
            message_type=MessageType.REQUEST,
            correlation_id=1,
            header=b"h",
            body=b"b",
        )

        # Act / Assert
        with pytest.raises(AttributeError):
            msg.body = b"other"  # type: ignore[misc]

    def test_equality_field_based(self) -> None:
        # Act
        first = TransportMessage(MessageType.REQUEST, 42, b"h", b"b")
        second = TransportMessage(MessageType.REQUEST, 42, b"h", b"b")

        # Assert
        assert first == second

    def test_hashable_and_usable_as_dict_key(self) -> None:
        # Arrange
        msg = TransportMessage(MessageType.ONE_WAY, 0, b"", b"x")

        # Act
        mapping = {msg: "one"}

        # Assert
        assert mapping[TransportMessage(MessageType.ONE_WAY, 0, b"", b"x")] == "one"


class TestTransportOptions:
    def test_defaults_match_architecture_doc(self) -> None:
        # Act
        options = TransportOptions()

        # Assert
        assert options.max_message_size == 16 * 1024 * 1024
        assert options.default_request_timeout == 30.0
        assert options.max_in_flight_requests == 1000
        assert options.keepalive_interval == 30.0
        assert options.keepalive_timeout == 10.0
        assert options.reconnect_base_delay == 0.1
        assert options.reconnect_max_delay == 30.0
        assert options.reconnect_jitter_fraction == 0.3
        assert options.connect_timeout == 5.0
        assert options.handshake_timeout == 5.0
        assert options.ssl_context is None

    def test_default_network_is_asyncio_network(self) -> None:
        # Act
        options = TransportOptions()

        # Assert
        assert isinstance(options.network, AsyncioNetwork)

    def test_network_can_be_overridden_with_in_memory(self) -> None:
        # Arrange
        memory_network = InMemoryNetwork()

        # Act
        options = TransportOptions(network=memory_network)

        # Assert
        assert options.network is memory_network


class TestTransportErrorHierarchy:
    def test_all_subclass_transport_error(self) -> None:
        # Assert
        assert issubclass(HandshakeError, TransportError)
        assert issubclass(MessageTooLargeError, TransportError)
        assert issubclass(BackpressureError, TransportError)
        assert issubclass(TransportTimeoutError, TransportError)
        assert issubclass(TransportConnectionError, TransportError)
        assert issubclass(TransportClosedError, TransportError)

    def test_timeout_is_a_stdlib_timeout_error(self) -> None:
        # Assert
        assert issubclass(TransportTimeoutError, TimeoutError)

    def test_connection_error_is_a_stdlib_connection_error(self) -> None:
        # Assert
        assert issubclass(TransportConnectionError, ConnectionError)

    def test_runtime_can_catch_timeout_via_stdlib_type(self) -> None:
        # Arrange / Act / Assert
        with pytest.raises(TimeoutError):
            raise TransportTimeoutError("timed out")

    def test_runtime_can_catch_connection_error_via_stdlib_type(self) -> None:
        # Arrange / Act / Assert
        with pytest.raises(ConnectionError):
            raise TransportConnectionError("lost")


class TestClusterTransportAbc:
    def test_abc_cannot_be_instantiated(self) -> None:
        # Act / Assert
        with pytest.raises(TypeError):
            IClusterTransport()  # type: ignore[abstract]

    def test_required_abstract_methods(self) -> None:
        # Act
        abstracts = IClusterTransport.__abstractmethods__

        # Assert
        assert {
            "start",
            "stop",
            "connect_to_silo",
            "disconnect_from_silo",
            "send_request",
            "send_one_way",
            "send_ping",
            "is_connected_to",
            "get_connected_silos",
            "local_silo",
            "on_connection_established",
            "on_connection_lost",
        } <= abstracts


class TestTransportFactoryAbc:
    def test_abc_cannot_be_instantiated(self) -> None:
        # Act / Assert
        with pytest.raises(TypeError):
            ITransportFactory()  # type: ignore[abstract]

    def test_create_cluster_transport_is_abstract(self) -> None:
        # Assert
        assert "create_cluster_transport" in ITransportFactory.__abstractmethods__


class TestCallbackSignatures:
    def test_on_connection_established_signature_accepts_callback(self) -> None:
        # Arrange
        sig = inspect.signature(IClusterTransport.on_connection_established)

        # Act
        params = list(sig.parameters)

        # Assert
        assert params == ["self", "callback"]

    def test_on_connection_lost_signature_accepts_callback(self) -> None:
        # Arrange
        sig = inspect.signature(IClusterTransport.on_connection_lost)

        # Act
        params = list(sig.parameters)

        # Assert
        assert params == ["self", "callback"]
