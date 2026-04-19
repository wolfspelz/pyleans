"""Cluster-transport handshake codec.

The handshake is exchanged immediately after TCP/TLS establishment and
before any :class:`TransportMessage` can flow. It proves that the peer
speaks the same protocol version, belongs to the same cluster, and — for
outbound connections where the initiator knows which silo it is dialling
— identifies as the expected silo (mitigating MITM when TLS is off).

Wire layout (big-endian) — see
:doc:`../../../docs/architecture/pyleans-transport` §4.1::

    [4 bytes]  magic number       0x504C4E53 ("PLNS")
    [2 bytes]  protocol version   uint16
    [4 bytes]  cluster id length  uint32
    [N bytes]  cluster id         UTF-8
    [4 bytes]  silo id length     uint32
    [N bytes]  silo id            UTF-8
    [4 bytes]  role flags         uint32 bitset (bit 0 = gateway-capable)
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from pyleans.cluster.identity import ClusterId
from pyleans.identity import SiloAddress
from pyleans.transport._io import readexactly_or_raise
from pyleans.transport.errors import HandshakeError

MAGIC = 0x504C4E53
PROTOCOL_VERSION = 1
MAX_HANDSHAKE_STRING = 256
ROLE_FLAG_GATEWAY_CAPABLE = 1 << 0
_RESERVED_ROLE_FLAGS_MASK = ~ROLE_FLAG_GATEWAY_CAPABLE & 0xFFFFFFFF

_MAGIC_STRUCT = struct.Struct(">I")
_VERSION_STRUCT = struct.Struct(">H")
_LENGTH_STRUCT = struct.Struct(">I")


@dataclass(frozen=True)
class Handshake:
    """Decoded handshake payload."""

    protocol_version: int
    cluster_id: str
    silo_id: str
    role_flags: int


def encode_handshake(handshake: Handshake) -> bytes:
    """Serialise ``handshake`` to its big-endian wire representation."""
    _validate_string_length("cluster_id", handshake.cluster_id)
    _validate_string_length("silo_id", handshake.silo_id)
    if not 0 <= handshake.role_flags <= 0xFFFFFFFF:
        raise ValueError(f"role_flags {handshake.role_flags!r} is not a uint32")
    cluster_bytes = handshake.cluster_id.encode("utf-8")
    silo_bytes = handshake.silo_id.encode("utf-8")
    parts = [
        _MAGIC_STRUCT.pack(MAGIC),
        _VERSION_STRUCT.pack(handshake.protocol_version),
        _LENGTH_STRUCT.pack(len(cluster_bytes)),
        cluster_bytes,
        _LENGTH_STRUCT.pack(len(silo_bytes)),
        silo_bytes,
        _LENGTH_STRUCT.pack(handshake.role_flags),
    ]
    return b"".join(parts)


async def read_handshake(reader: asyncio.StreamReader, timeout: float) -> Handshake:
    """Read and validate the handshake frame within ``timeout`` seconds."""
    try:
        return await asyncio.wait_for(_read_handshake_body(reader), timeout=timeout)
    except TimeoutError as exc:
        raise HandshakeError(f"handshake did not complete within {timeout}s") from exc


def validate_peer_handshake(
    peer: Handshake,
    expected_cluster: ClusterId,
    expected_silo: SiloAddress | None = None,
) -> None:
    """Raise :class:`HandshakeError` if ``peer`` is not an acceptable partner."""
    if peer.protocol_version != PROTOCOL_VERSION:
        raise HandshakeError(
            f"protocol version mismatch: expected {PROTOCOL_VERSION}, got {peer.protocol_version}"
        )
    if peer.cluster_id != expected_cluster.value:
        raise HandshakeError(
            f"cluster id mismatch: expected {expected_cluster.value!r}, got {peer.cluster_id!r}"
        )
    if expected_silo is not None and peer.silo_id != expected_silo.silo_id:
        raise HandshakeError(
            f"silo id mismatch: expected {expected_silo.silo_id!r}, got {peer.silo_id!r}"
        )
    if peer.role_flags & _RESERVED_ROLE_FLAGS_MASK:
        raise HandshakeError(f"reserved role-flag bits set: {peer.role_flags:#010x}")


async def _read_handshake_body(reader: asyncio.StreamReader) -> Handshake:
    magic_bytes = await _readexactly(reader, _MAGIC_STRUCT.size)
    (magic,) = _MAGIC_STRUCT.unpack(magic_bytes)
    if magic != MAGIC:
        raise HandshakeError(f"magic number mismatch: got {magic:#010x}")
    version_bytes = await _readexactly(reader, _VERSION_STRUCT.size)
    (version,) = _VERSION_STRUCT.unpack(version_bytes)
    cluster_id = await _read_bounded_string(reader, "cluster_id")
    silo_id = await _read_bounded_string(reader, "silo_id")
    flags_bytes = await _readexactly(reader, _LENGTH_STRUCT.size)
    (role_flags,) = _LENGTH_STRUCT.unpack(flags_bytes)
    return Handshake(
        protocol_version=version,
        cluster_id=cluster_id,
        silo_id=silo_id,
        role_flags=role_flags,
    )


async def _read_bounded_string(reader: asyncio.StreamReader, field_name: str) -> str:
    length_bytes = await _readexactly(reader, _LENGTH_STRUCT.size)
    (length,) = _LENGTH_STRUCT.unpack(length_bytes)
    if length > MAX_HANDSHAKE_STRING:
        raise HandshakeError(
            f"{field_name} length {length} exceeds MAX_HANDSHAKE_STRING {MAX_HANDSHAKE_STRING}"
        )
    payload = await _readexactly(reader, length)
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HandshakeError(f"{field_name} is not valid UTF-8") from exc


async def _readexactly(reader: asyncio.StreamReader, count: int) -> bytes:
    return await readexactly_or_raise(reader, count, _truncation_to_handshake_error)


def _truncation_to_handshake_error(
    exc: asyncio.IncompleteReadError, expected: int
) -> HandshakeError:
    return HandshakeError(
        f"peer closed during handshake: expected {expected} bytes, got {len(exc.partial)}"
    )


def _validate_string_length(field_name: str, value: str) -> None:
    encoded_length = len(value.encode("utf-8"))
    if encoded_length > MAX_HANDSHAKE_STRING:
        raise ValueError(
            f"{field_name} encoded length {encoded_length} "
            f"exceeds MAX_HANDSHAKE_STRING {MAX_HANDSHAKE_STRING}"
        )
