# Task 17: Counter Client (CLI Tool)

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-16-counter-app.md](task-16-counter-app.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Section 7 (Client usage)

## Description

Create a command-line tool `counter-client` that interacts with the counter-app
via HTTP. Demonstrates how external clients call grains.

### Files to create
- `examples/counter-client/counter_client.py`
- `examples/counter-client/requirements.txt`

### CLI interface

```bash
# Get current value
python counter_client.py get my-counter
# Output: Counter 'my-counter': 42

# Increment
python counter_client.py inc my-counter
# Output: Counter 'my-counter': 43

# Set value
python counter_client.py set my-counter 100
# Output: Counter 'my-counter': 100
```

### Implementation

```python
#!/usr/bin/env python3
"""CLI client for the counter-app silo."""

import argparse
import httpx
import sys

DEFAULT_BASE_URL = "http://localhost:8000"

def main():
    parser = argparse.ArgumentParser(description="Counter grain CLI client")
    parser.add_argument("command", choices=["get", "inc", "set"],
                        help="Command to execute")
    parser.add_argument("counter_id", help="Counter grain ID")
    parser.add_argument("value", nargs="?", type=int,
                        help="Value for 'set' command")
    parser.add_argument("--url", default=DEFAULT_BASE_URL,
                        help="Base URL of the counter-app")
    args = parser.parse_args()

    with httpx.Client(base_url=args.url) as client:
        if args.command == "get":
            resp = client.get(f"/counter/{args.counter_id}")
        elif args.command == "inc":
            resp = client.post(f"/counter/{args.counter_id}/increment")
        elif args.command == "set":
            if args.value is None:
                print("Error: 'set' requires a value", file=sys.stderr)
                sys.exit(1)
            resp = client.put(f"/counter/{args.counter_id}",
                              params={"value": args.value})

        resp.raise_for_status()
        data = resp.json()
        print(f"Counter '{data['counter_id']}': {data['value']}")

if __name__ == "__main__":
    main()
```

### Dependencies
- `httpx` -- modern HTTP client for Python

### Acceptance criteria

- [ ] `python counter_client.py get foo` shows current value
- [ ] `python counter_client.py inc foo` increments and shows new value
- [ ] `python counter_client.py set foo 42` sets and confirms value
- [ ] `--url` flag allows connecting to different host/port
- [ ] Error message if counter-app is not running
- [ ] Error message if 'set' called without value
- [ ] Exit code 0 on success, non-zero on error

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._