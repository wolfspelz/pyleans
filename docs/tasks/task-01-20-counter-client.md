# Task 01-20: Counter Client (Gateway Protocol)

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-19-counter-app.md](task-01-19-counter-app.md)
- [task-01-15-network-port.md](task-01-15-network-port.md) -- `ClusterClient` accepts an `INetwork` for injection at test time
- [task-01-16-in-memory-network-simulator.md](task-01-16-in-memory-network-simulator.md) -- end-to-end client tests use the simulator

## References
- [adr-package-split](../adr/adr-package-split.md) -- client library usage pattern
- [pyleans-transport.md](../architecture/pyleans-transport.md) -- Gateway protocol

## Description

Create a command-line tool `counter-client` that interacts with the
counter-app silo via the pyleans client library and the gateway protocol.
Demonstrates how external processes call grains without being a silo.

### Files to create
- `src/counter_client/main.py`
- `src/counter_client/__init__.py`
- `src/counter_client/pyproject.toml (removed — sample apps now live under src/ and are run as `python -m src.counter_client`)`

### CLI interface

```bash
# Get current value
python -m src.counter_client get my-counter
# Output: Counter 'my-counter': 42

# Increment
python -m src.counter_client inc my-counter
# Output: Counter 'my-counter': 43

# Set value
python -m src.counter_client set my-counter 100
# Output: Counter 'my-counter': 100

# Query silo info (via any counter grain)
python -m src.counter_client info my-counter
# Output:
# Silo info (via 'my-counter'):
#   silo_id: localhost_11111_1713180000
#   host: localhost
#   hostname: DESKTOP-ABC
#   gateway_port: 30000
#   grain_count: 3
#   uptime_seconds: 42.5
#   ...
```

### Implementation sketch

```python
"""CLI client for the counter-app silo using the pyleans client library."""

import argparse
import asyncio
import sys

from pyleans.client import ClusterClient

DEFAULT_GATEWAY = "localhost:30000"

async def run(args: argparse.Namespace) -> None:
    # network defaults to AsyncioNetwork for CLI use (real TCP). Tests inject
    # InMemoryNetwork — see task-01-15 and task-01-16.
    client = ClusterClient(gateways=[args.gateway])
    await client.connect()

    # Import grain class for type reference
    from src.counter_app.counter_grain import CounterGrain

    counter = client.get_grain(CounterGrain, args.counter_id)

    if args.command == "get":
        value = await counter.get_value()
    elif args.command == "inc":
        value = await counter.increment()
    elif args.command == "set":
        if args.value is None:
            print("Error: 'set' requires a value", file=sys.stderr)
            sys.exit(1)
        await counter.set_value(args.value)
        value = args.value
    elif args.command == "info":
        info = await counter.get_silo_info()
        print(f"Silo info (via '{args.counter_id}'):")
        for k, v in info.items():
            print(f"  {k}: {v}")
        await client.close()
        return

    print(f"Counter '{args.counter_id}': {value}")
    await client.close()

def main() -> None:
    parser = argparse.ArgumentParser(description="Counter grain CLI client")
    parser.add_argument("command", choices=["get", "inc", "set", "info"],
                        help="Command to execute")
    parser.add_argument("counter_id", help="Counter grain ID")
    parser.add_argument("value", nargs="?", type=int,
                        help="Value for 'set' command")
    parser.add_argument("--gateway", default=DEFAULT_GATEWAY,
                        help="Gateway address (host:port)")
    args = parser.parse_args()
    asyncio.run(run(args))

if __name__ == "__main__":
    main()
```

### Prerequisites

This task requires the pyleans client library (`pyleans.client`) to implement:
- `ClusterClient` — connects to a silo gateway
- `client.get_grain()` — returns a `GrainRef` that dispatches calls over the gateway protocol

If the gateway protocol is not yet implemented (Phase 2 transport), this task
should implement a minimal in-process or local-socket gateway for Phase 1 that
allows the client to call grains on a locally-running silo.

### Dependencies
- `pyleans` (client library)

### Acceptance criteria

- [x] `python -m src.counter_client get foo` shows current value
- [x] `python -m src.counter_client inc foo` increments and shows new value
- [x] `python -m src.counter_client set foo 42` sets and confirms value
- [x] `python -m src.counter_client info foo` shows silo metadata via CounterGrain
- [x] `--gateway` flag allows connecting to different silo
- [x] Error message if silo is not running
- [x] Error message if 'set' called without value
- [x] Exit code 0 on success, non-zero on error
- [x] Uses `pyleans.client` library, not raw HTTP
- [x] `ClusterClient.__init__` accepts `network: INetwork | None = None`; default `AsyncioNetwork`. Constructing it with `network=InMemoryNetwork()` (shared with a silo in the same test) routes the grain call through the simulator with no OS ports bound
- [x] End-to-end client tests use the `network` pytest fixture from [task-01-16](task-01-16-in-memory-network-simulator.md); the "silo not running" error case is covered by `ConnectionRefusedError` raised from `InMemoryNetwork.open_connection` against an unbound virtual address — no hardcoded port literals needed

## Findings of code review

- No issues found. Code follows CLAUDE.md standards: clean naming, SOLID, type hints, hexagonal architecture.
- `_serialize_result` in listener does an extra `orjson.dumps` round-trip for non-primitives; acceptable since it catches serialization errors at the right layer.

## Findings of security review

- No authentication on the gateway TCP listener — acceptable for Phase 1 (local dev mode). Phase 2 transport doc specifies TLS.
- Frame size bounded to 16 MB (`MAX_FRAME_SIZE`) — prevents unbounded memory allocation.
- Frame length validated in `read_frame` before allocation — no buffer overflow.
- Gateway dispatches validate required fields — missing fields return errors, not crashes.
- JSON-only deserialization (orjson) — no code execution risk.
- Connection cleanup via `try/finally` — no socket leaks.
- No per-client rate limiting — acceptable for Phase 1.

## Summary of implementation

### Files created
- `src/pyleans/pyleans/gateway/__init__.py` — gateway module exports
- `src/pyleans/pyleans/gateway/protocol.py` — binary frame encoding/decoding following the documented wire format
- `src/pyleans/pyleans/gateway/listener.py` — `GatewayListener` TCP server that dispatches grain calls to runtime
- `src/pyleans/pyleans/client/cluster_client.py` — `ClusterClient` and `RemoteGrainRef` for remote grain calls
- `src/counter_client/__init__.py` — package marker
- `src/counter_client/main.py` — CLI with get/inc/set commands
- `src/counter_client/__main__.py` — `python -m src.counter_client` entry point
- `src/pyleans/test/test_gateway_protocol.py` — 14 tests for frame encoding/decoding
- `src/pyleans/test/test_gateway.py` — 12 tests for gateway listener + client integration
- `src/counter_client/test/test_counter_client.py` — 9 tests for CLI and client

### Files modified
- `src/pyleans/pyleans/server/silo.py` — added `gateway_port` parameter, wired `GatewayListener` into start/stop
- `src/pyleans/pyleans/client/__init__.py` — exports `ClusterClient` and `RemoteGrainRef`
- `src/counter_client/pyproject.toml (removed — sample apps now live under src/ and are run as `python -m src.counter_client`)` — added `asyncio_mode` for pytest
- `src/pyleans/test/test_silo.py` — added `gateway_port=0` to test helper
- `src/counter_app/test/test_counter_grain.py` — added `gateway_port=0` to test helper
- `src/counter_app/test/test_counter_app.py` — added `gateway_port=0` to test helper

### Key decisions
- Phase 1 gateway uses a minimal TCP protocol with length-prefixed binary framing (matches the documented wire format from pyleans-transport.md)
- JSON payloads for simplicity (Phase 2 may switch to more efficient format)
- `gateway_port=0` in tests for OS-assigned ports to avoid conflicts
- `RemoteGrainRef` mirrors the `GrainRef` API so client code looks identical to in-process code
- `ClusterClient` uses a single TCP connection with multiplexed requests via correlation IDs

### Test coverage (35 new tests, 302 total)
- **test_gateway_protocol.py** (14): MessageType values, encode/decode roundtrip, frame structure, error cases (short frame, unknown type, invalid JSON, oversized frames)
- **test_gateway.py** (12): connect/disconnect, remote grain calls (increment, set, concurrent), error propagation, unknown grains, state persistence across client sessions
- **test_counter_client.py** (9): get/inc/set commands via ClusterClient, CLI `run()` output, error exits (no silo, missing value)
