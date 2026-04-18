# Task 23: Wire Protocol -- Frame Codec and Connection Handshake

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-19-cluster-identity.md](task-19-cluster-identity.md)
- [task-22-transport-abcs.md](task-22-transport-abcs.md)

## References
- [architecture/pyleans-transport.md](../architecture/pyleans-transport.md) -- §4.1 handshake, §4.3 framing, §4.4 correlation
- [orleans-networking.md](../orleans-architecture/orleans-networking.md) -- §5 message format on the wire

## Description

Everything on the wire is length-prefixed binary. This task implements **pure codec functions**: no sockets, no asyncio, no connection state. The codec is consumed by [task-24](task-24-silo-connection.md) to read/write frames over an `asyncio.StreamReader` / `StreamWriter`, and by [task-25](task-25-silo-connection-manager.md) to perform the handshake.

Isolating the codec from I/O makes it straightforward to unit-test every bit-twiddle without spinning up real sockets and gives us a single place to harden against malformed input -- every frame the transport ever sees passes through these functions.

### Files to create

- `src/pyleans/pyleans/transport/wire.py` -- frame encoder/decoder, `FrameHeader` dataclass
- `src/pyleans/pyleans/transport/handshake.py` -- handshake encoder/decoder, validation

### Frame format

From [architecture/pyleans-transport.md §4.3](../architecture/pyleans-transport.md), big-endian throughout:

```
Wire frame:
[4 bytes]  total frame length (uint32, excludes these 4 bytes)
[1 byte]   message type      (MessageType enum value)
[8 bytes]  correlation id    (uint64)
[4 bytes]  header length     (uint32)
[H bytes]  header payload
[B bytes]  body payload     (B = total - 13 - H)
```

```python
# wire.py
from dataclasses import dataclass
from pyleans.transport.messages import MessageType, TransportMessage

FRAME_PREFIX_SIZE = 4            # 4-byte total length
FRAME_OVERHEAD_AFTER_PREFIX = 13 # type(1) + corr(8) + header_len(4)


def encode_frame(msg: TransportMessage, max_size: int) -> bytes:
    """Encode a message into a single length-prefixed frame.

    Raises MessageTooLargeError if the resulting frame exceeds max_size.
    Raises ValueError on out-of-range fields.
    """


async def read_frame(
    reader: asyncio.StreamReader, max_size: int
) -> TransportMessage:
    """Read exactly one frame from the stream.

    Raises:
        MessageTooLargeError: declared total length exceeds max_size.
        TransportConnectionError: peer closed before frame completed
            (wraps asyncio.IncompleteReadError).
        ValueError: malformed fields (unknown message type,
            header_length > total).
    """
```

### Why the length prefix is first and separate

Reading a 4-byte prefix with `readexactly(4)` means the adapter can size-check the upcoming frame **before** allocating buffer space for it. A peer that claims a 2 GB frame gets rejected immediately without the reader committing to an allocation. Without the separate prefix, the adapter would have to stream and count -- slower and more complex.

### Handshake format

Silo-to-silo handshake, exchanged immediately after TCP/TLS establishment, before any `TransportMessage` can flow:

```
[4 bytes]  magic number      0x504C4E53 ("PLNS")
[2 bytes]  protocol version  uint16 (currently 1)
[4 bytes]  cluster_id length uint32
[N bytes]  cluster_id bytes  UTF-8
[4 bytes]  silo_id length    uint32
[N bytes]  silo_id bytes     UTF-8   (format: "host:port:epoch")
[4 bytes]  role flags        uint32 bitset (bit 0 = gateway-capable, reserved bits MUST be zero)
```

```python
# handshake.py
MAGIC = 0x504C4E53
PROTOCOL_VERSION = 1
MAX_HANDSHAKE_STRING = 256  # bytes, prevents memory exhaustion via crafted handshake


@dataclass(frozen=True)
class Handshake:
    protocol_version: int
    cluster_id: str
    silo_id: str
    role_flags: int


def encode_handshake(h: Handshake) -> bytes: ...


async def read_handshake(
    reader: asyncio.StreamReader, timeout: float
) -> Handshake:
    """Read and validate magic+version+fields within timeout.

    Raises HandshakeError on magic mismatch, version mismatch,
    oversized strings, or timeout.
    """


def validate_peer_handshake(
    peer: Handshake,
    expected_cluster: ClusterId,
    expected_silo: SiloAddress | None = None,
) -> None:
    """Raise HandshakeError if peer is not an acceptable mesh partner.

    Checks:
      - protocol_version == PROTOCOL_VERSION (no backward-compat during PoC)
      - cluster_id matches expected_cluster (blocks cross-cluster accidents)
      - if expected_silo is provided (we initiated the connection), the
        peer MUST identify as exactly that silo, mitigating MITM in
        non-TLS deployments.
    """
```

### Version policy

Protocol version is hardcoded to 1. Any mismatch is a hard `HandshakeError` during the PoC -- no negotiation, no degraded modes. When Phase 4 introduces a second version, this is the file to change; making a deliberate decision beats soft-failing and running mismatched protocols with subtle bugs.

### Hardening checklist (why each bound exists)

- `max_size` enforced at `read_frame` entry: prevents OOM from a malicious or buggy peer that declares a multi-GB frame.
- `MAX_HANDSHAKE_STRING` cap on cluster_id and silo_id lengths: same threat, different vector.
- Reserved bits in `role_flags` MUST be zero on the wire: ensures forward-compat bits aren't silently ignored (future version can signal rejection when an old silo sees new flags).
- All integer fields validated against known ranges before being cast to enum values (`MessageType(v)` raises `ValueError` on unknown codepoints, which we convert to a `HandshakeError`/`TransportError` as appropriate).
- Magic number is checked before any length field is read: pure-text probe traffic (e.g. accidentally connecting to an HTTP server) fails fast with a precise error instead of allocating against random bytes.

### Error payload encoding

`MessageType.ERROR` responses carry a structured body so the caller can reconstruct an appropriate exception without leaking internals:

```
body = [4 bytes]  error code  (uint32 — see task-22 error hierarchy → numeric mapping)
       [4 bytes]  reason length
       [N bytes]  reason (UTF-8, truncated to 1024 bytes)
```

Mapping:

| Code | Maps to |
|---|---|
| 1 | `TransportError` (generic) |
| 2 | `HandshakeError` |
| 3 | `MessageTooLargeError` |
| 4 | `TransportTimeoutError` |
| 5 | `TransportConnectionError` |
| 100+ | Application error (reason is the stringified exception — task-34 decides whether to reconstruct) |

Application errors start at 100 so the transport always knows whether the error came from itself or the grain above.

### Acceptance criteria

- [ ] `encode_frame` + `read_frame` round-trip arbitrary `TransportMessage` values (property-test-style with hypothesis-lite assertions)
- [ ] `read_frame` raises `MessageTooLargeError` when the length prefix exceeds `max_size` (without reading the body)
- [ ] `read_frame` raises `TransportConnectionError` when the peer closes mid-frame
- [ ] `encode_handshake` + `read_handshake` round-trip
- [ ] `read_handshake` rejects wrong magic, wrong version, oversized strings
- [ ] `validate_peer_handshake` rejects cluster-id mismatch and expected-silo mismatch
- [ ] Unit tests cover each error path plus a "truncated stream" case that produces `TransportConnectionError`
- [ ] Frame overhead math documented and unit-tested (`len(encoded) == FRAME_PREFIX_SIZE + total_length`)

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
