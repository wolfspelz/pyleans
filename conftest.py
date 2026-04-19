"""Project-root pytest fixtures shared across all test packages.

Why a root conftest: keeping fixtures here (rather than duplicating them in
each testpath's conftest.py) avoids the ``conftest`` module-name collision
that occurs when multiple same-named conftests live in sibling directories
under a single pytest invocation.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pyleans.net import InMemoryNetwork


@pytest.fixture
def network() -> Iterator[InMemoryNetwork]:
    """One ``InMemoryNetwork`` per test — zero OS ports bound.

    Canonical fixture from ``adr-network-port-for-testability``. Pass it to
    ``Silo(network=network)`` and ``ClusterClient(network=network)`` so the
    test runs entirely in-process.
    """
    yield InMemoryNetwork()
