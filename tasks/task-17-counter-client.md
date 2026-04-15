# Task 17: Counter Client (Gateway Protocol)

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-16-counter-app.md](task-16-counter-app.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Section 7 (Client usage)
- [pyleans-transport.md](../docs/pyleans-transport.md) -- Gateway protocol

## Description

Create a command-line tool `counter-client` that interacts with the
counter-app silo via the pyleans client library and the gateway protocol.
Demonstrates how external processes call grains without being a silo.

### Files to create
- `counter-client/counter_client/main.py`
- `counter-client/counter_client/__init__.py`
- `counter-client/pyproject.toml`

### CLI interface

```bash
# Get current value
python -m counter_client get my-counter
# Output: Counter 'my-counter': 42

# Increment
python -m counter_client inc my-counter
# Output: Counter 'my-counter': 43

# Set value
python -m counter_client set my-counter 100
# Output: Counter 'my-counter': 100
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
    client = ClusterClient(gateways=[args.gateway])
    await client.connect()

    # Import grain class for type reference
    from counter.grains import CounterGrain

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

    print(f"Counter '{args.counter_id}': {value}")
    await client.close()

def main() -> None:
    parser = argparse.ArgumentParser(description="Counter grain CLI client")
    parser.add_argument("command", choices=["get", "inc", "set"],
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

- [ ] `python -m counter_client get foo` shows current value
- [ ] `python -m counter_client inc foo` increments and shows new value
- [ ] `python -m counter_client set foo 42` sets and confirms value
- [ ] `--gateway` flag allows connecting to different silo
- [ ] Error message if silo is not running
- [ ] Error message if 'set' called without value
- [ ] Exit code 0 on success, non-zero on error
- [ ] Uses `pyleans.client` library, not raw HTTP

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
