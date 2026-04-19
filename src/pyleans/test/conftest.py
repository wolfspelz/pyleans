"""Shared test fixtures and helpers for pyleans tests."""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any

import pytest
from pyleans.providers.storage import StorageProvider

_FORBIDDEN_ASYNCIO_NET_NAMES = frozenset({"start_server", "open_connection"})


def scan_for_forbidden_asyncio_net_calls(path: Path) -> list[str]:
    """Return ``asyncio.{start_server,open_connection}`` references in *path*.

    AST-based so string/comment occurrences are ignored. Test modules must
    use :class:`pyleans.net.INetwork` (with ``InMemoryNetwork`` for the
    simulator or ``AsyncioNetwork`` for the adapter's own smoke tests).

    See ``docs/adr/adr-network-port-for-testability.md``.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return []

    findings: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _FORBIDDEN_ASYNCIO_NET_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id == "asyncio"
        ):
            findings.append(f"asyncio.{node.attr} at line {node.lineno}")
        elif isinstance(node, ast.ImportFrom) and node.module == "asyncio":
            for alias in node.names:
                if alias.name in _FORBIDDEN_ASYNCIO_NET_NAMES:
                    findings.append(f"from asyncio import {alias.name} at line {node.lineno}")
    return findings


def pytest_collectstart(collector: pytest.Collector) -> None:
    """Fail collection for any test module that bypasses the INetwork port.

    Skips ``test_asyncio_network.py`` which legitimately exercises the
    production adapter end-to-end via ``AsyncioNetwork`` (and therefore
    does not trip the scanner because it doesn't call asyncio directly).
    """
    if not isinstance(collector, pytest.Module):
        return
    module_path = Path(str(collector.fspath))
    if not module_path.name.startswith("test_"):
        return
    findings = scan_for_forbidden_asyncio_net_calls(module_path)
    if findings:
        joined = ", ".join(findings)
        pytest.fail(
            f"{module_path.name}: uses asyncio networking directly ({joined}). "
            "Tests must use pyleans.net.INetwork — typically InMemoryNetwork via the "
            "`network` fixture. See docs/adr/adr-network-port-for-testability.md.",
            pytrace=False,
        )


class FakeStorageProvider(StorageProvider):
    """In-memory storage for testing."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict[str, Any], str]] = {}

    async def read(self, grain_type: str, grain_key: str) -> tuple[dict[str, Any], str | None]:
        key = f"{grain_type}/{grain_key}"
        if key in self._store:
            state, etag = self._store[key]
            return state, etag
        return {}, None

    async def write(
        self,
        grain_type: str,
        grain_key: str,
        state: dict[str, Any],
        expected_etag: str | None,
    ) -> str:
        key = f"{grain_type}/{grain_key}"
        new_etag = str(time.monotonic())
        self._store[key] = (state, new_etag)
        return new_etag

    async def clear(
        self,
        grain_type: str,
        grain_key: str,
        expected_etag: str | None,
    ) -> None:
        key = f"{grain_type}/{grain_key}"
        self._store.pop(key, None)
