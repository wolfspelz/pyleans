# Task 02-05: Wire Protocol -- Frame Codec and Connection Handshake

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-01-cluster-identity.md](task-02-01-cluster-identity.md)
- [task-02-04-transport-abcs.md](task-02-04-transport-abcs.md)

## References
- [architecture/pyleans-transport.md](../architecture/pyleans-transport.md) -- §4.1 handshake, §4.3 framing, §4.4 correlation
- [orleans-networking.md](../orleans-architecture/orleans-networking.md) -- §5 message format on the wire

## Description

Everything on the wire is length-prefixed binary. This task implements **pure codec functions**: no sockets, no asyncio, no connection state. The codec is consumed by [task-02-06](task-02-06-silo-connection.md) to read/write frames over an `asyncio.StreamReader` / `StreamWriter`, and by [task-02-07](task-02-07-silo-connection-manager.md) to perform the handshake.

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
body = [4 bytes]  error code  (uint32 — see task-02-04 error hierarchy → numeric mapping)
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
| 100+ | Application error (reason is the stringified exception — task-02-16 decides whether to reconstruct) |

Application errors start at 100 so the transport always knows whether the error came from itself or the grain above.

### Acceptance criteria

- [x] `encode_frame` + `read_frame` round-trip arbitrary `TransportMessage` values
- [x] `read_frame` raises `MessageTooLargeError` when the length prefix exceeds `max_size` (without reading the body)
- [x] `read_frame` raises `TransportConnectionError` when the peer closes mid-frame
- [x] `encode_handshake` + `read_handshake` round-trip
- [x] `read_handshake` rejects wrong magic, wrong version (via `validate_peer_handshake`), oversized strings
- [x] `validate_peer_handshake` rejects cluster-id mismatch and expected-silo mismatch
- [x] Unit tests cover each error path plus a "truncated stream" case that produces `TransportConnectionError`
- [x] Frame overhead math documented and unit-tested (`len(encoded) == FRAME_PREFIX_SIZE + total_length`)

## Findings of code review

- **Clean code / SOLID**: pure codec modules; no I/O state; every helper single-purpose. `wire.py`, `handshake.py`, and `error_payload.py` each own exactly one wire concern. `_io.py` is a private shared helper — a ruff/pylint `duplicate-code` warning forced the extraction; it now has one implementation of `readexactly_or_raise` that both codecs call.
- **Type hints**: fully typed; `code_to_exception_class` returns `type[Exception]`; `readexactly_or_raise` takes a `Callable[[IncompleteReadError, int], Exception]`. `mypy` strict clean.
- **Hardening** (see security review) is implemented in the codec itself — max-size checks, bounded string lengths, reserved-bit enforcement.
- **Tests**: 47 tests across wire (21), handshake (14), error-payload (12). AAA labels, one Act per test; every error path has its own test.
- **Logging**: none in the codec — codecs are synchronous pure functions called on every frame; adapters log at their own boundaries.
- **Naming**: modules `wire`, `handshake`, `error_payload`; public identifiers `encode_*`, `read_*`, `decode_*`, `validate_*`. `Handshake`, `FrameHeader`, `ErrorPayload`, `ErrorCode` PascalCase. `MAGIC`, `PROTOCOL_VERSION`, `FRAME_PREFIX_SIZE`, `FRAME_OVERHEAD_AFTER_PREFIX`, `MAX_HANDSHAKE_STRING`, `MAX_REASON_BYTES`, `APPLICATION_ERROR_BASE` UPPER_SNAKE.

No open issues.

## Findings of security review

- **Length-prefix bound check before body read**: `read_frame` rejects frames whose declared length exceeds `max_size` *before* allocating the body — a peer claiming a 2 GB frame never causes an allocation.
- **Bounded handshake strings**: `MAX_HANDSHAKE_STRING = 256` bytes on both `cluster_id` and `silo_id`; oversized lengths raise `HandshakeError` before bytes are read.
- **Magic-number check first**: any non-`PLNS` peer is rejected immediately — HTTP probes or misrouted connections fail loudly instead of allocating against random bytes.
- **Reserved-bit enforcement**: `validate_peer_handshake` rejects any role-flag bit outside the currently defined set. New bits added in future versions will not be silently ignored by older silos.
- **Unknown message types**: `MessageType(value)` raises `ValueError` on unknown codepoints; `read_frame` converts that into a diagnostic `ValueError("unknown message type 0xNN")` that the connection layer (task-02-06) can turn into a connection-close.
- **Error-payload bounds**: reason bytes are capped at `MAX_REASON_BYTES = 1024`; decode rejects declared lengths that exceed the buffer or the cap.
- **No file, network, or subprocess I/O** in any codec module; all network I/O is performed by the connection layer consumers.

No vulnerabilities found.

## Summary of implementation

### Files created

- [src/pyleans/pyleans/transport/wire.py](../../src/pyleans/pyleans/transport/wire.py) — `FrameHeader`, `encode_frame`, `read_frame`, `decode_frame_prefix`, `FRAME_PREFIX_SIZE`, `FRAME_OVERHEAD_AFTER_PREFIX`.
- [src/pyleans/pyleans/transport/handshake.py](../../src/pyleans/pyleans/transport/handshake.py) — `Handshake`, `encode_handshake`, `read_handshake`, `validate_peer_handshake`, `MAGIC`, `PROTOCOL_VERSION`, `MAX_HANDSHAKE_STRING`, `ROLE_FLAG_GATEWAY_CAPABLE`.
- [src/pyleans/pyleans/transport/error_payload.py](../../src/pyleans/pyleans/transport/error_payload.py) — `ErrorCode`, `ErrorPayload`, `encode_error_payload`, `decode_error_payload`, `exception_to_code`, `code_to_exception_class`.
- [src/pyleans/pyleans/transport/_io.py](../../src/pyleans/pyleans/transport/_io.py) — private `readexactly_or_raise` helper shared by wire + handshake codecs (resolves the pylint `duplicate-code` note).
- [src/pyleans/pyleans/transport/__init__.py](../../src/pyleans/pyleans/transport/__init__.py) — re-exports every public codec symbol.
- [src/pyleans/test/test_transport_wire.py](../../src/pyleans/test/test_transport_wire.py) — 21 tests.
- [src/pyleans/test/test_transport_handshake.py](../../src/pyleans/test/test_transport_handshake.py) — 14 tests.
- [src/pyleans/test/test_transport_error_payload.py](../../src/pyleans/test/test_transport_error_payload.py) — 12 tests.

### Key decisions

- **Length prefix separate from the fixed preamble.** The codec reads the 4-byte prefix, bounds-checks, then commits to the 13-byte preamble and the two payload slices. That matches the hardening checklist in the task spec and means a pathological peer gets rejected after four bytes.
- **`struct.Struct` instances at module scope** (`_FRAME_PREFIX`, `_FRAME_FIXED`, `_MAGIC_STRUCT`, `_VERSION_STRUCT`, `_LENGTH_STRUCT`, `_HEADER_STRUCT`). Pre-compiled structs avoid per-frame parsing of the format string and make the layout explicit at module load.
- **Handshake read/validate split.** `read_handshake` returns a `Handshake` dataclass; `validate_peer_handshake` applies policy (cluster mismatch, expected-silo mismatch, reserved-bit violation). This mirrors task-02-07 where the connection manager runs validation after the silo is known. It also lets tests assert that a wrong-version handshake *parses* successfully (so diagnostics are meaningful) and is rejected only at validation.
- **`ErrorPayload` as first-class domain type.** Task 02-16 will reconstruct exceptions from `MessageType.ERROR` responses; having a named payload with `is_application_error` avoids duplicating magic-number checks there.
- **Shared `_io.readexactly_or_raise` helper**. Extracted to kill a pylint `duplicate-code` note; the helper takes a `make_error` callback so each codec maps truncation to its own exception type without duplicating the try/except.
- **Reserved role-flag bits**: `_RESERVED_ROLE_FLAGS_MASK = ~ROLE_FLAG_GATEWAY_CAPABLE & 0xFFFFFFFF` enforces that any future-reserved bit seen on the wire is rejected — old silos can't silently ignore new capabilities that would change peer semantics.

### Deviations

None. The codec matches the task spec's frame layout, handshake format, and error payload exactly. The private `_io.py` helper is an internal refactor prompted by pylint's `duplicate-code` note, not a deviation.

### Test coverage summary

47 tests:
- **wire** (21): frame overhead math; prefix↔length round-trip; max-size rejection on encode; correlation-id range; round-trip for empty, populated, every message type, and 68 KB payloads; `read_frame` error paths for oversized prefix, peer-close-mid-frame, EOF-before-prefix, unknown message type, header > payload region, declared length shorter than overhead; `decode_frame_prefix` parsing / wrong-size prefix rejection.
- **handshake** (14): magic prefix on encode; oversized cluster / silo / flags on encode; round-trip via `asyncio.StreamReader`; wrong magic, version-parsed-but-rejected-by-validator, oversized cluster length, peer-close-mid-handshake, timeout; `validate_peer_handshake` happy path, cluster mismatch, expected-silo mismatch / match, reserved-bit rejection.
- **error_payload** (12): round-trip for transport / empty-reason / truncated-to-max / application codes; `is_application_error` on both sides of the threshold; buffer-too-short / truncated-reason / reason-over-max decode errors; encode range checks; `exception_to_code` full transport-error table; `code_to_exception_class` transport codes, unknown codes, application codes.

Full suite: **562 tests** pass; `ruff check` clean, `ruff format --check` clean, `pylint` 10.00/10, `mypy` strict clean.
