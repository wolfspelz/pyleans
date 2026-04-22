"""CLI client for the counter-app silo using the pyleans client library."""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import shlex
import sys
from typing import TYPE_CHECKING

from pyleans.client import ClusterClient
from pyleans.identity import SiloInfo, SiloStatus
from pyleans.net import INetwork
from pyleans.providers.membership import MembershipProvider
from pyleans.server.providers.markdown_table_membership import (
    MarkdownTableMembershipProvider,
)

if TYPE_CHECKING:
    from pyleans.client import RemoteGrainRef

DEFAULT_MEMBERSHIP = "./data/membership.md"
DEFAULT_CLUSTER_ID = "dev"
DEFAULT_LOG_LEVEL = "INFO"

_ONE_SHOT_COMMANDS = {"get", "inc", "set", "info", "reload"}
_INTERACTIVE_EXIT = {"quit", "exit"}
_INTERACTIVE_HELP = {"help", "?"}


async def _pick_gateways_from_membership(
    membership: MembershipProvider,
    cluster_id: str,
) -> list[str]:
    """Read the membership table and return active-silo gateways as ``host:port``.

    Only silos that have announced a gateway port and carry the requested
    cluster id are eligible. The list is shuffled so successive client
    invocations distribute across the cluster instead of hammering one
    gateway; :class:`ClusterClient` still falls back to the next address
    on connection error.
    """
    snapshot = await membership.read_all()
    eligible: list[SiloInfo] = [
        s
        for s in snapshot.silos
        if s.status == SiloStatus.ACTIVE
        and s.gateway_port is not None
        and (s.cluster_id == cluster_id or s.cluster_id is None)
    ]
    if not eligible:
        raise RuntimeError(
            f"No active silos with a gateway port found for cluster_id={cluster_id!r}",
        )
    random.shuffle(eligible)
    return [f"{s.address.host}:{s.gateway_port}" for s in eligible]


async def _resolve_gateways_async(args: argparse.Namespace) -> list[str]:
    """Determine gateway addresses — prefer explicit ``--gateway`` flags.

    If none were given, consult the membership file for the active silos in
    the cluster and use their gateway ports in randomised order.
    """
    if args.gateway:
        return list(args.gateway)
    provider = MarkdownTableMembershipProvider(args.membership)
    return await _pick_gateways_from_membership(provider, args.cluster_id)


def _build_client(
    args: argparse.Namespace,
    gateways: list[str],
    network: INetwork | None,
) -> ClusterClient:
    """Build a :class:`ClusterClient`, wiring a membership-driven refresher in
    membership-table mode so a broken gateway prompts a failover to another
    active silo.
    """
    if args.gateway:
        return ClusterClient(gateways=gateways, network=network)
    provider = MarkdownTableMembershipProvider(args.membership)

    async def refresher() -> list[str]:
        return await _pick_gateways_from_membership(provider, args.cluster_id)

    return ClusterClient(
        gateways=gateways,
        network=network,
        gateway_refresher=refresher,
    )


async def _execute_command(
    client: ClusterClient,
    command: str,
    counter_id: str,
    value: int | None,
) -> bool:
    """Run one grain-facing command through ``client``. Returns False on error.

    Side effect: prints the result (or error) to stdout / stderr. The caller
    handles SystemExit vs. continuing in interactive mode.
    """
    from src.counter_app.counter_grain import CounterGrain

    counter: RemoteGrainRef = client.get_grain(CounterGrain, counter_id)

    if command == "info":
        info = await counter.get_silo_info()
        print(f"Silo info (via '{counter_id}'):")
        for key, val in info.items():
            print(f"  {key}: {val}")
        return True

    if command == "get":
        result = await counter.get_value()
    elif command == "inc":
        result = await counter.increment()
    elif command == "reload":
        result = await counter.reload()
    elif command == "set":
        if value is None:
            print("Error: 'set' requires a value", file=sys.stderr)
            return False
        await counter.set_value(value)
        result = value
    else:
        print(f"Error: unknown command {command!r}", file=sys.stderr)
        return False

    print(f"Counter '{counter_id}': {result}")
    return True


async def _run_one_shot(client: ClusterClient, args: argparse.Namespace) -> None:
    ok = await _execute_command(client, args.command, args.counter_id, args.value)
    if not ok:
        sys.exit(1)


def _print_interactive_banner(client: ClusterClient) -> None:
    print("counter_client interactive mode — type 'help' for commands, 'quit' to exit.")
    print(f"connected to {client.connected_gateway}")


def _print_interactive_help() -> None:
    print("Commands:")
    print("  get <counter_id>")
    print("  inc <counter_id>")
    print("  set <counter_id> <value>")
    print("  reload <counter_id>")
    print("  info <counter_id>")
    print("  status           — show the currently connected gateway")
    print("  help | ?         — print this help")
    print("  quit | exit      — leave interactive mode")


def _parse_interactive_line(line: str) -> tuple[str, list[str]] | None:
    """Tokenise one user-entered line; return ``(command, args)`` or ``None`` to skip."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        print(f"Error: could not parse {stripped!r}: {exc}", file=sys.stderr)
        return ("__invalid__", [])
    if not tokens:
        return None
    return (tokens[0], tokens[1:])


async def _read_line(prompt: str) -> str | None:
    """Read one line from stdin off the event-loop thread; ``None`` on EOF."""
    loop = asyncio.get_running_loop()

    def _sync_input() -> str | None:
        try:
            return input(prompt)
        except EOFError:
            return None

    return await loop.run_in_executor(None, _sync_input)


async def _handle_interactive_command(
    client: ClusterClient,
    command: str,
    tokens: list[str],
) -> None:
    """Dispatch one interactive-mode input line."""
    if command == "status":
        if tokens:
            print("Error: 'status' takes no arguments", file=sys.stderr)
            return
        print(f"connected_gateway: {client.connected_gateway}")
        return
    if command in _ONE_SHOT_COMMANDS:
        if not tokens:
            print(f"Error: '{command}' requires a counter_id", file=sys.stderr)
            return
        counter_id = tokens[0]
        value: int | None = None
        if command == "set":
            if len(tokens) < 2:
                print("Error: 'set' requires a value", file=sys.stderr)
                return
            try:
                value = int(tokens[1])
            except ValueError:
                print(f"Error: 'set' value must be an integer, got {tokens[1]!r}", file=sys.stderr)
                return
        try:
            await _execute_command(client, command, counter_id, value)
        except ConnectionError as exc:
            print(f"Error: command failed — no active silo reachable ({exc})", file=sys.stderr)
        return
    print(f"Error: unknown command {command!r}. Type 'help'.", file=sys.stderr)


async def _run_interactive(client: ClusterClient) -> None:
    """Read-eval-print loop until EOF or a quit command."""
    _print_interactive_banner(client)
    while True:
        try:
            line = await _read_line("> ")
        except (asyncio.CancelledError, KeyboardInterrupt):
            print()
            return
        if line is None:
            print()
            return
        parsed = _parse_interactive_line(line)
        if parsed is None:
            continue
        command, tokens = parsed
        if command == "__invalid__":
            continue
        if command in _INTERACTIVE_EXIT:
            return
        if command in _INTERACTIVE_HELP:
            _print_interactive_help()
            continue
        await _handle_interactive_command(client, command, tokens)


async def run(args: argparse.Namespace, *, network: INetwork | None = None) -> None:
    """Connect to the silo, then either execute one command or enter the REPL."""
    try:
        gateways = await _resolve_gateways_async(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    client = _build_client(args, gateways, network)
    try:
        await client.connect()
    except ConnectionError as e:
        print(f"Error: could not connect to any gateway {gateways}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.command is None:
            await _run_interactive(client)
        else:
            await _run_one_shot(client, args)
    finally:
        await client.close()


def main() -> None:
    """Parse arguments and run the async command."""
    parser = argparse.ArgumentParser(description="Counter grain CLI client")
    parser.add_argument(
        "command",
        nargs="?",
        default=None,
        choices=[*sorted(_ONE_SHOT_COMMANDS), None],
        help=(
            "Command to execute. Omit to enter interactive mode, where "
            "commands are read one-per-line from stdin until 'quit'."
        ),
    )
    parser.add_argument(
        "counter_id",
        nargs="?",
        default=None,
        help="Counter grain ID (required for one-shot mode)",
    )
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
            "If omitted, the client reads --membership and picks an "
            "active silo at random."
        ),
    )
    parser.add_argument(
        "--membership",
        default=DEFAULT_MEMBERSHIP,
        help=(
            f"Path to the shared membership file (default: {DEFAULT_MEMBERSHIP}). "
            "Consulted when --gateway is not provided."
        ),
    )
    parser.add_argument(
        "--cluster-id",
        dest="cluster_id",
        default=DEFAULT_CLUSTER_ID,
        help=(
            f"Cluster identifier used to filter membership rows "
            f"(default: {DEFAULT_CLUSTER_ID}). Only active silos advertising "
            "this cluster_id (or no cluster_id) are eligible."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        help=f"Root logger level (default: {DEFAULT_LOG_LEVEL})",
    )
    args = parser.parse_args()
    if args.command is not None and args.counter_id is None:
        parser.error(f"'{args.command}' requires a counter_id")
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
