"""Tests for pyleans.transport.handshake."""

from __future__ import annotations

import asyncio

import pytest
from pyleans.cluster import ClusterId
from pyleans.identity import SiloAddress
from pyleans.transport.errors import HandshakeError
from pyleans.transport.handshake import (
    MAGIC,
    MAX_HANDSHAKE_STRING,
    PROTOCOL_VERSION,
    ROLE_FLAG_GATEWAY_CAPABLE,
    Handshake,
    encode_handshake,
    read_handshake,
    validate_peer_handshake,
)


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _handshake(
    *,
    version: int = PROTOCOL_VERSION,
    cluster: str = "demo",
    silo: str = "10.0.0.1:11111:1700000000",
    flags: int = ROLE_FLAG_GATEWAY_CAPABLE,
) -> Handshake:
    return Handshake(
        protocol_version=version,
        cluster_id=cluster,
        silo_id=silo,
        role_flags=flags,
    )


class TestEncode:
    def test_encodes_magic_prefix(self) -> None:
        # Arrange
        handshake = _handshake()

        # Act
        encoded = encode_handshake(handshake)

        # Assert
        assert encoded[:4] == MAGIC.to_bytes(4, "big")

    def test_oversized_cluster_id_raises(self) -> None:
        # Arrange
        handshake = _handshake(cluster="x" * (MAX_HANDSHAKE_STRING + 1))

        # Act / Assert
        with pytest.raises(ValueError):
            encode_handshake(handshake)

    def test_oversized_silo_id_raises(self) -> None:
        # Arrange
        handshake = _handshake(silo="s" * (MAX_HANDSHAKE_STRING + 1))

        # Act / Assert
        with pytest.raises(ValueError):
            encode_handshake(handshake)

    def test_role_flags_outside_uint32_raises(self) -> None:
        # Arrange
        handshake = _handshake(flags=2**33)

        # Act / Assert
        with pytest.raises(ValueError):
            encode_handshake(handshake)


class TestReadRoundTrip:
    @pytest.mark.asyncio
    async def test_round_trip_via_stream(self) -> None:
        # Arrange
        handshake = _handshake()

        # Act
        decoded = await read_handshake(_reader(encode_handshake(handshake)), timeout=1.0)

        # Assert
        assert decoded == handshake

    @pytest.mark.asyncio
    async def test_wrong_magic_raises_handshake_error(self) -> None:
        # Arrange
        encoded = bytearray(encode_handshake(_handshake()))
        encoded[:4] = b"\x00\x00\x00\x00"

        # Act / Assert
        with pytest.raises(HandshakeError, match="magic number mismatch"):
            await read_handshake(_reader(bytes(encoded)), timeout=1.0)

    @pytest.mark.asyncio
    async def test_wrong_version_accepted_but_rejected_by_validator(self) -> None:
        # Arrange — read parses the version; validation rejects
        encoded = encode_handshake(_handshake(version=PROTOCOL_VERSION + 1))

        # Act
        decoded = await read_handshake(_reader(encoded), timeout=1.0)

        # Assert
        assert decoded.protocol_version == PROTOCOL_VERSION + 1
        with pytest.raises(HandshakeError, match="protocol version mismatch"):
            validate_peer_handshake(decoded, ClusterId("demo"))

    @pytest.mark.asyncio
    async def test_oversized_cluster_id_raises(self) -> None:
        # Arrange
        parts = [
            MAGIC.to_bytes(4, "big"),
            PROTOCOL_VERSION.to_bytes(2, "big"),
            (MAX_HANDSHAKE_STRING + 1).to_bytes(4, "big"),
        ]

        # Act / Assert
        with pytest.raises(HandshakeError, match="cluster_id length"):
            await read_handshake(_reader(b"".join(parts)), timeout=1.0)

    @pytest.mark.asyncio
    async def test_peer_closes_mid_handshake_raises_handshake_error(self) -> None:
        # Arrange
        truncated = encode_handshake(_handshake())[:-2]

        # Act / Assert
        with pytest.raises(HandshakeError, match="peer closed"):
            await read_handshake(_reader(truncated), timeout=1.0)

    @pytest.mark.asyncio
    async def test_timeout_raises_handshake_error(self) -> None:
        # Arrange
        reader = asyncio.StreamReader()

        # Act / Assert
        with pytest.raises(HandshakeError, match="did not complete"):
            await read_handshake(reader, timeout=0.01)


class TestValidatePeerHandshake:
    def test_happy_path(self) -> None:
        # Act / Assert (no exception)
        validate_peer_handshake(_handshake(), ClusterId("demo"))

    def test_cluster_id_mismatch_raises(self) -> None:
        # Arrange
        handshake = _handshake(cluster="other")

        # Act / Assert
        with pytest.raises(HandshakeError, match="cluster id mismatch"):
            validate_peer_handshake(handshake, ClusterId("demo"))

    def test_expected_silo_mismatch_raises(self) -> None:
        # Arrange
        handshake = _handshake(silo="10.0.0.9:11111:1700000000")
        expected = SiloAddress(host="10.0.0.1", port=11111, epoch=1700000000)

        # Act / Assert
        with pytest.raises(HandshakeError, match="silo id mismatch"):
            validate_peer_handshake(handshake, ClusterId("demo"), expected)

    def test_expected_silo_match_passes(self) -> None:
        # Arrange
        expected = SiloAddress(host="10.0.0.1", port=11111, epoch=1700000000)

        # Act / Assert (no exception)
        validate_peer_handshake(_handshake(), ClusterId("demo"), expected)

    def test_reserved_flag_bit_raises(self) -> None:
        # Arrange
        handshake = _handshake(flags=1 << 5)

        # Act / Assert
        with pytest.raises(HandshakeError, match="reserved role-flag bits"):
            validate_peer_handshake(handshake, ClusterId("demo"))
