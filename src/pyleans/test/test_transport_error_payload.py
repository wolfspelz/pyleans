"""Tests for pyleans.transport.error_payload."""

import pytest
from pyleans.transport.error_payload import (
    APPLICATION_ERROR_BASE,
    MAX_REASON_BYTES,
    ErrorCode,
    ErrorPayload,
    code_to_exception_class,
    decode_error_payload,
    encode_error_payload,
    exception_to_code,
)
from pyleans.transport.errors import (
    HandshakeError,
    MessageTooLargeError,
    TransportConnectionError,
    TransportError,
    TransportTimeoutError,
)


class TestRoundTrip:
    def test_round_trip_transport_error(self) -> None:
        # Arrange
        payload = ErrorPayload(code=ErrorCode.TRANSPORT_ERROR, reason="boom")

        # Act
        decoded = decode_error_payload(encode_error_payload(payload))

        # Assert
        assert decoded == payload

    def test_round_trip_empty_reason(self) -> None:
        # Arrange
        payload = ErrorPayload(code=ErrorCode.TRANSPORT_TIMEOUT_ERROR, reason="")

        # Act
        decoded = decode_error_payload(encode_error_payload(payload))

        # Assert
        assert decoded == payload

    def test_long_reason_is_truncated(self) -> None:
        # Arrange
        payload = ErrorPayload(
            code=ErrorCode.APPLICATION_ERROR, reason="x" * (MAX_REASON_BYTES + 100)
        )

        # Act
        decoded = decode_error_payload(encode_error_payload(payload))

        # Assert
        assert len(decoded.reason.encode("utf-8")) == MAX_REASON_BYTES

    def test_application_error_flag(self) -> None:
        # Arrange
        payload = ErrorPayload(code=APPLICATION_ERROR_BASE + 7, reason="app")

        # Act / Assert
        assert payload.is_application_error

    def test_transport_error_is_not_application_error(self) -> None:
        # Arrange
        payload = ErrorPayload(code=ErrorCode.HANDSHAKE_ERROR, reason="h")

        # Act / Assert
        assert not payload.is_application_error


class TestDecodeErrorPaths:
    def test_rejects_too_short_buffer(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="at least"):
            decode_error_payload(b"\x00\x00")

    def test_rejects_truncated_reason(self) -> None:
        # Arrange
        corrupt = (
            (ErrorCode.TRANSPORT_ERROR.value).to_bytes(4, "big")
            + (10).to_bytes(4, "big")
            + b"short"
        )

        # Act / Assert
        with pytest.raises(ValueError, match="exceeds remaining"):
            decode_error_payload(corrupt)

    def test_rejects_reason_length_over_limit(self) -> None:
        # Arrange
        big = (
            (ErrorCode.TRANSPORT_ERROR.value).to_bytes(4, "big")
            + (MAX_REASON_BYTES + 1).to_bytes(4, "big")
            + b"x" * (MAX_REASON_BYTES + 1)
        )

        # Act / Assert
        with pytest.raises(ValueError, match="exceeds MAX_REASON_BYTES"):
            decode_error_payload(big)


class TestEncodeBounds:
    def test_rejects_negative_code(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            encode_error_payload(ErrorPayload(code=-1, reason="x"))

    def test_rejects_oversized_code(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            encode_error_payload(ErrorPayload(code=2**33, reason="x"))


class TestMapping:
    def test_exception_to_code_transport_errors(self) -> None:
        # Assert
        assert exception_to_code(HandshakeError("h")) == ErrorCode.HANDSHAKE_ERROR
        assert exception_to_code(MessageTooLargeError("m")) == ErrorCode.MESSAGE_TOO_LARGE_ERROR
        assert exception_to_code(TransportTimeoutError("t")) == ErrorCode.TRANSPORT_TIMEOUT_ERROR
        assert (
            exception_to_code(TransportConnectionError("c")) == ErrorCode.TRANSPORT_CONNECTION_ERROR
        )
        assert exception_to_code(TransportError("g")) == ErrorCode.TRANSPORT_ERROR

    def test_exception_to_code_application_error_default(self) -> None:
        # Assert
        assert exception_to_code(ValueError("boom")) == APPLICATION_ERROR_BASE

    def test_code_to_exception_class_transport_codes(self) -> None:
        # Assert
        assert code_to_exception_class(ErrorCode.HANDSHAKE_ERROR) is HandshakeError
        assert code_to_exception_class(ErrorCode.TRANSPORT_TIMEOUT_ERROR) is (TransportTimeoutError)
        assert code_to_exception_class(ErrorCode.TRANSPORT_ERROR) is TransportError

    def test_code_to_exception_class_unknown_code_returns_transport_error(self) -> None:
        # Assert
        assert code_to_exception_class(42) is TransportError

    def test_application_code_returns_transport_error(self) -> None:
        # Assert
        assert code_to_exception_class(APPLICATION_ERROR_BASE + 5) is TransportError
