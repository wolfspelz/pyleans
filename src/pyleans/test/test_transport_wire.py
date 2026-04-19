"""Tests for pyleans.transport.wire — frame encoder / decoder."""

from __future__ import annotations

import asyncio

import pytest
from pyleans.transport.errors import MessageTooLargeError, TransportConnectionError
from pyleans.transport.messages import MessageType, TransportMessage
from pyleans.transport.wire import (
    FRAME_OVERHEAD_AFTER_PREFIX,
    FRAME_PREFIX_SIZE,
    decode_frame_prefix,
    encode_frame,
    read_frame,
)

DEFAULT_MAX = 1024 * 1024


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def _decode(data: bytes, max_size: int = DEFAULT_MAX) -> TransportMessage:
    return await read_frame(_reader(data), max_size)


class TestEncode:
    def test_frame_overhead_math(self) -> None:
        # Arrange
        msg = TransportMessage(MessageType.REQUEST, 0, b"", b"")

        # Act
        encoded = encode_frame(msg, DEFAULT_MAX)

        # Assert
        assert len(encoded) == FRAME_PREFIX_SIZE + FRAME_OVERHEAD_AFTER_PREFIX

    def test_encoded_length_matches_prefix(self) -> None:
        # Arrange
        msg = TransportMessage(MessageType.RESPONSE, 42, b"hdr", b"body-bytes")

        # Act
        encoded = encode_frame(msg, DEFAULT_MAX)
        prefix_length = decode_frame_prefix(encoded[:FRAME_PREFIX_SIZE])

        # Assert
        assert len(encoded) == FRAME_PREFIX_SIZE + prefix_length

    def test_exceeds_max_size_raises(self) -> None:
        # Arrange
        body = b"x" * 2048
        msg = TransportMessage(MessageType.REQUEST, 1, b"", body)

        # Act / Assert
        with pytest.raises(MessageTooLargeError):
            encode_frame(msg, 256)

    def test_negative_correlation_id_raises_value_error(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            encode_frame(TransportMessage(MessageType.REQUEST, -1, b"", b""), DEFAULT_MAX)

    def test_correlation_id_over_uint64_raises(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            encode_frame(TransportMessage(MessageType.REQUEST, 2**64, b"", b""), DEFAULT_MAX)


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_round_trip_empty_payloads(self) -> None:
        # Arrange
        msg = TransportMessage(MessageType.ONE_WAY, 0, b"", b"")

        # Act
        decoded = await _decode(encode_frame(msg, DEFAULT_MAX))

        # Assert
        assert decoded == msg

    @pytest.mark.asyncio
    async def test_round_trip_with_header_and_body(self) -> None:
        # Arrange
        msg = TransportMessage(MessageType.REQUEST, 7, b"header-bytes", b"body-bytes")

        # Act
        decoded = await _decode(encode_frame(msg, DEFAULT_MAX))

        # Assert
        assert decoded == msg

    @pytest.mark.asyncio
    async def test_round_trip_every_message_type(self) -> None:
        # Arrange / Act
        decoded_types = []
        for message_type in MessageType:
            msg = TransportMessage(message_type, 3, b"h", b"b")
            decoded = await _decode(encode_frame(msg, DEFAULT_MAX))
            decoded_types.append(decoded.message_type)

        # Assert
        assert decoded_types == list(MessageType)

    @pytest.mark.asyncio
    async def test_round_trip_large_payload(self) -> None:
        # Arrange
        header = b"h" * 4096
        body = b"b" * 65536
        msg = TransportMessage(MessageType.RESPONSE, 42, header, body)

        # Act
        decoded = await _decode(encode_frame(msg, 1024 * 1024), 1024 * 1024)

        # Assert
        assert decoded == msg


class TestReadFrameErrorPaths:
    @pytest.mark.asyncio
    async def test_prefix_exceeds_max_size_raises_without_reading_body(self) -> None:
        # Arrange
        msg = TransportMessage(MessageType.REQUEST, 1, b"", b"x" * 2048)
        encoded = encode_frame(msg, DEFAULT_MAX)
        reader = _reader(encoded[:FRAME_PREFIX_SIZE])

        # Act / Assert
        with pytest.raises(MessageTooLargeError):
            await read_frame(reader, FRAME_PREFIX_SIZE + FRAME_OVERHEAD_AFTER_PREFIX)

    @pytest.mark.asyncio
    async def test_peer_closed_mid_frame_raises_connection_error(self) -> None:
        # Arrange
        msg = TransportMessage(MessageType.REQUEST, 1, b"hdr", b"body-bytes")
        encoded = encode_frame(msg, DEFAULT_MAX)
        truncated = encoded[: len(encoded) - 5]

        # Act / Assert
        with pytest.raises(TransportConnectionError):
            await _decode(truncated)

    @pytest.mark.asyncio
    async def test_eof_before_prefix_raises_connection_error(self) -> None:
        # Arrange
        reader = _reader(b"")

        # Act / Assert
        with pytest.raises(TransportConnectionError):
            await read_frame(reader, DEFAULT_MAX)

    @pytest.mark.asyncio
    async def test_unknown_message_type_raises_value_error(self) -> None:
        # Arrange
        corrupt_byte = 0xAA
        msg = TransportMessage(MessageType.REQUEST, 1, b"", b"")
        encoded = bytearray(encode_frame(msg, DEFAULT_MAX))
        encoded[FRAME_PREFIX_SIZE] = corrupt_byte

        # Act / Assert
        with pytest.raises(ValueError, match="unknown message type"):
            await _decode(bytes(encoded))

    @pytest.mark.asyncio
    async def test_header_length_exceeds_payload_region_raises_value_error(self) -> None:
        # Arrange
        msg = TransportMessage(MessageType.REQUEST, 1, b"hdr", b"body")
        encoded = bytearray(encode_frame(msg, DEFAULT_MAX))
        header_length_offset = FRAME_PREFIX_SIZE + 1 + 8
        encoded[header_length_offset : header_length_offset + 4] = b"\xff\xff\xff\xff"

        # Act / Assert
        with pytest.raises(ValueError, match="exceeds payload region"):
            await _decode(bytes(encoded))

    @pytest.mark.asyncio
    async def test_declared_total_smaller_than_overhead_raises(self) -> None:
        # Arrange
        encoded = (5).to_bytes(4, "big")

        # Act / Assert
        with pytest.raises(ValueError, match="shorter than overhead"):
            await _decode(encoded)


class TestDecodeFramePrefix:
    def test_matches_encoded_length(self) -> None:
        # Arrange
        msg = TransportMessage(MessageType.RESPONSE, 9, b"h", b"b")
        encoded = encode_frame(msg, DEFAULT_MAX)

        # Act
        length = decode_frame_prefix(encoded[:FRAME_PREFIX_SIZE])

        # Assert
        assert FRAME_PREFIX_SIZE + length == len(encoded)

    def test_rejects_wrong_size_prefix(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError):
            decode_frame_prefix(b"\x00\x00")
