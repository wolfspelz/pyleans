"""Tests for :mod:`pyleans.server.remote_invoke`.

Covers the wire codec (request + success + failure), exception
wire-format helpers, and bounds on message-size fields.
"""

from __future__ import annotations

import pytest
from pyleans.identity import GrainId
from pyleans.server.remote_invoke import (
    GRAIN_CALL_HEADER,
    GrainCallFailure,
    GrainCallRequest,
    GrainCallSuccess,
    decode_request,
    decode_response,
    encode_failure,
    encode_request,
    encode_success,
    format_exception_for_wire,
)


def _request() -> GrainCallRequest:
    return GrainCallRequest(
        grain_id=GrainId("CounterGrain", "c1"),
        method="increment",
        args=[3],
        kwargs={"reason": "test"},
        caller_silo="a:11111:1",
        call_id=42,
        deadline=1_700_000_050.25,
    )


class TestRequestCodec:
    def test_roundtrip_preserves_fields(self) -> None:
        # Arrange
        req = _request()

        # Act
        wire = encode_request(req)
        back = decode_request(wire)

        # Assert
        assert back == req

    def test_header_is_stable_constant(self) -> None:
        # Act / Assert
        assert GRAIN_CALL_HEADER == b"pyleans/grain-call/v1"

    def test_rejects_unsupported_schema_version(self) -> None:
        # Arrange - forge a body with bad version
        body = b'{"v":99,"grain_type":"T","grain_key":"k","method":"m","args":[],"kwargs":{}}'

        # Act / Assert
        with pytest.raises(ValueError, match="schema"):
            decode_request(body)

    def test_rejects_malformed_bytes(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="invalid grain-call bytes"):
            decode_request(b"\xffnot json")

    def test_rejects_missing_fields(self) -> None:
        # Arrange
        body = b'{"v":1,"grain_type":"T"}'

        # Act / Assert
        with pytest.raises(ValueError, match="grain-call fields"):
            decode_request(body)


class TestResponseCodec:
    def test_success_roundtrip(self) -> None:
        # Arrange
        wire = encode_success(result={"answer": 42})

        # Act
        resp = decode_response(wire)

        # Assert
        assert isinstance(resp, GrainCallSuccess)
        assert resp.result == {"answer": 42}

    def test_failure_roundtrip(self) -> None:
        # Arrange
        wire = encode_failure("ValueError", "bad input", "Traceback...")

        # Act
        resp = decode_response(wire)

        # Assert
        assert isinstance(resp, GrainCallFailure)
        assert resp.exception_type == "ValueError"
        assert resp.message == "bad input"
        assert resp.remote_traceback == "Traceback..."

    def test_failure_without_traceback(self) -> None:
        # Arrange
        wire = encode_failure("KeyError", "missing")

        # Act
        resp = decode_response(wire)

        # Assert
        assert isinstance(resp, GrainCallFailure)
        assert resp.remote_traceback is None

    def test_message_truncated_at_cap(self) -> None:
        # Arrange
        huge = "x" * 20_000

        # Act
        wire = encode_failure("E", huge)
        resp = decode_response(wire)

        # Assert
        assert isinstance(resp, GrainCallFailure)
        assert len(resp.message) <= 8 * 1024


class TestExceptionFormatter:
    def test_extracts_type_message_and_traceback(self) -> None:
        # Arrange
        try:
            raise ValueError("oops")
        except ValueError as exc:
            exc_type, message, tb = format_exception_for_wire(exc)

        # Assert
        assert exc_type == "ValueError"
        assert message == "oops"
        assert "ValueError" in tb
