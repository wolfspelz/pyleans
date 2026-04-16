"""CLI client for the counter-app silo using the pyleans client library."""

import argparse
import asyncio
import sys

from pyleans.client import ClusterClient

DEFAULT_GATEWAY = "localhost:30000"


async def run(args: argparse.Namespace) -> None:
    """Connect to the silo, execute the command, and print the result."""
    client = ClusterClient(gateways=[args.gateway])
    try:
        await client.connect()
    except ConnectionError as e:
        print(f"Error: could not connect to silo at {args.gateway}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        from counter_app.counter_grain import CounterGrain

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
            for key, val in info.items():
                print(f"  {key}: {val}")
            return

        print(f"Counter '{args.counter_id}': {value}")
    finally:
        await client.close()


def main() -> None:
    """Parse arguments and run the async command."""
    parser = argparse.ArgumentParser(description="Counter grain CLI client")
    parser.add_argument(
        "command",
        choices=["get", "inc", "set", "info"],
        help="Command to execute",
    )
    parser.add_argument("counter_id", help="Counter grain ID")
    parser.add_argument(
        "value",
        nargs="?",
        type=int,
        help="Value for 'set' command",
    )
    parser.add_argument(
        "--gateway",
        default=DEFAULT_GATEWAY,
        help=f"Gateway address host:port (default: {DEFAULT_GATEWAY})",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
