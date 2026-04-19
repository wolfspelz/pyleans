"""CLI client for the counter-app silo using the pyleans client library."""

import argparse
import asyncio
import sys

from pyleans.client import ClusterClient
from pyleans.net import INetwork

DEFAULT_GATEWAY = "localhost:30000"


def _resolve_gateways(args: argparse.Namespace) -> list[str]:
    """Accept either a single ``--gateway`` or a repeated list of them."""
    raw = getattr(args, "gateway", DEFAULT_GATEWAY)
    if isinstance(raw, list):
        return list(raw)
    return [raw]


async def run(args: argparse.Namespace, *, network: INetwork | None = None) -> None:
    """Connect to the silo, execute the command, and print the result."""
    gateways = _resolve_gateways(args)
    client = ClusterClient(gateways=gateways, network=network)
    try:
        await client.connect()
    except ConnectionError as e:
        print(f"Error: could not connect to any gateway {gateways}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        from src.counter_app.counter_grain import CounterGrain

        counter = client.get_grain(CounterGrain, args.counter_id)

        if args.command == "info":
            info = await counter.get_silo_info()
            print(f"Silo info (via '{args.counter_id}'):")
            for key, val in info.items():
                print(f"  {key}: {val}")
            return

        if args.command == "get":
            value = await counter.get_value()
        elif args.command == "inc":
            value = await counter.increment()
        else:  # "set"
            if args.value is None:
                print("Error: 'set' requires a value", file=sys.stderr)
                sys.exit(1)
            await counter.set_value(args.value)
            value = args.value

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
        action="append",
        help=(
            "Gateway address host:port. May be repeated; the client "
            "falls back to the next address on connection error. "
            f"(default: {DEFAULT_GATEWAY})"
        ),
    )
    args = parser.parse_args()
    if not args.gateway:
        args.gateway = [DEFAULT_GATEWAY]
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
