"""Standalone silo hosting CounterGrain and AnswerGrain.

Run with defaults for the Phase 1 single-silo demo::

    python -m src.counter_app

Or with explicit ports and a shared membership file for the Phase 2
two-silo demo::

    python -m src.counter_app --port 11111 --gateway 30000 \\
        --membership ./data/membership.yaml --cluster-id dev

    python -m src.counter_app --port 11112 --gateway 30001 \\
        --membership ./data/membership.yaml --cluster-id dev

See :doc:`../../../docs/tasks/task-02-19-counter-sample-multi-silo` and
the README next to this module for the full walkthrough.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from pyleans.cluster.identity import ClusterId
from pyleans.server import Silo
from pyleans.server.grains import system_grains
from pyleans.server.providers import FileStorageProvider, MarkdownTableMembershipProvider

from src.counter_app.answer_grain import AnswerGrain
from src.counter_app.counter_grain import CounterGrain


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Counter-app silo")
    parser.add_argument("--host", default="localhost", help="Host to bind (default: localhost)")
    parser.add_argument(
        "--port",
        type=int,
        default=11111,
        help="Silo-to-silo TCP port (default: 11111)",
    )
    parser.add_argument(
        "--gateway",
        type=int,
        default=30000,
        help="Gateway TCP port (default: 30000)",
    )
    parser.add_argument(
        "--membership",
        default="./data/membership.md",
        help="Path to the shared membership file (default: ./data/membership.md)",
    )
    parser.add_argument(
        "--cluster-id",
        default="dev",
        help="Cluster identifier shared by all silos in the cluster (default: dev)",
    )
    parser.add_argument(
        "--storage-dir",
        default="./data/storage",
        help="Storage root directory (default: ./data/storage)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Root logger level (default: INFO)",
    )
    return parser.parse_args(argv)


async def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger = logging.getLogger("counter_app")
    logger.info(
        "Starting silo: host=%s port=%d gateway=%d cluster=%s membership=%s",
        args.host,
        args.port,
        args.gateway,
        args.cluster_id,
        args.membership,
    )
    silo = Silo(
        grains=[CounterGrain, AnswerGrain, *system_grains()],
        storage_providers={"default": FileStorageProvider(args.storage_dir)},
        membership_provider=MarkdownTableMembershipProvider(args.membership),
        host=args.host,
        port=args.port,
        gateway_port=args.gateway,
        cluster_id=ClusterId(args.cluster_id),
    )
    await silo.start()


if __name__ == "__main__":
    asyncio.run(main())
