"""Tests for the gateway binary protocol (frame encoding/decoding)."""

import asyncio
import struct

import pytest
from pyleans.errors import TransportError
from pyleans.gateway.protocol import (
    FRAME_PREFIX_SIZE,
    HEADER_SIZE,
    MAX_FRAME_SIZE,
    MIN_FRAME_SIZE,
    MessageType,
    decode_frame,
    encode_frame,
    read_frame,
)


class TestMessageType:
    def test_request_value(self) -> None:
        assert MessageType.REQUEST == 0x01

    def test_response_value(self) -> None:
        assert MessageType.RESPONSE == 0x02


class TestEncodeFrame:
    def test_encode_request(self) -> None:
        frame = encode_frame(MessageType.REQUEST, 1, {"method": "get"})
        assert isinstance(frame, bytes)
        assert len(frame) > HEADER_SIZE

    def test_encode_response(self) -> None:
        frame = encode_frame(MessageType.RESPONSE, 42, {"ok": True, "result": 5})
        assert isinstance(frame, bytes)

    def test_frame_starts_with_length(self) -> None:
        frame = encode_frame(MessageType.REQUEST, 1, {"test": True})
        (frame_len,) = struct.unpack("!I", frame[:4])
        assert frame_len == len(frame) - FRAME_PREFIX_SIZE

    def test_correlation_id_preserved(self) -> None:
        frame = encode_frame(MessageType.REQUEST, 99999, {"x": 1})
        _, correlation_id, _ = decode_frame(frame)
        assert correlation_id == 99999

    def test_empty_payload(self) -> None:
        frame = encode_frame(MessageType.REQUEST, 1, {})
        _, _, payload = decode_frame(frame)
        assert payload == {}


class TestDecodeFrame:
    def test_roundtrip(self) -> None:
        original = {"grain_type": "Counter", "key": "k1", "method": "inc"}
        frame = encode_frame(MessageType.REQUEST, 7, original)
        msg_type, corr_id, payload = decode_frame(frame)
        assert msg_type == MessageType.REQUEST
        assert corr_id == 7
        assert payload == original

    def test_frame_too_short_raises(self) -> None:
        with pytest.raises(TransportError, match="too short"):
            decode_frame(b"\x00\x00")

    def test_unknown_message_type_raises(self) -> None:
        # Build a valid-length frame with unknown message type 0x09
        payload = b"{}"
        frame_len = MIN_FRAME_SIZE + len(payload)
        data = struct.pack("!IbQ", frame_len, 0x09, 1) + payload
        with pytest.raises(TransportError, match="Unknown message type"):
            decode_frame(data)

    def test_invalid_json_raises(self) -> None:
        bad_json = b"not json"
        frame_len = MIN_FRAME_SIZE + len(bad_json)
        data = struct.pack("!IbQ", frame_len, 0x01, 1) + bad_json
        with pytest.raises(TransportError, match="Invalid JSON"):
            decode_frame(data)


class TestReadFrame:
    async def test_read_valid_frame(self) -> None:
        frame = encode_frame(MessageType.REQUEST, 1, {"key": "val"})
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        result = await read_frame(reader)
        assert result == frame

    async def test_read_multiple_frames(self) -> None:
        frame1 = encode_frame(MessageType.REQUEST, 1, {"a": 1})
        frame2 = encode_frame(MessageType.RESPONSE, 2, {"b": 2})
        reader = asyncio.StreamReader()
        reader.feed_data(frame1 + frame2)

        r1 = await read_frame(reader)
        r2 = await read_frame(reader)
        assert r1 == frame1
        assert r2 == frame2

    async def test_eof_raises_incomplete_read(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00")
        reader.feed_eof()
        with pytest.raises(asyncio.IncompleteReadError):
            await read_frame(reader)

    async def test_frame_length_too_small_raises(self) -> None:
        # Frame length = 0 (less than minimum)
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", 0))
        reader.feed_eof()
        with pytest.raises(TransportError, match="too small"):
            await read_frame(reader)

    async def test_frame_length_too_large_raises(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", MAX_FRAME_SIZE + 1))
        reader.feed_eof()
        with pytest.raises(TransportError, match="exceeds maximum"):
            await read_frame(reader)
