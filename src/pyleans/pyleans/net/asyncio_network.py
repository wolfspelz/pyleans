"""AsyncioNetwork — production ``INetwork`` adapter.

Thin pass-through to ``asyncio.start_server`` / ``asyncio.open_connection``.
Zero behavior change vs using asyncio directly.
"""

from __future__ import annotations

import asyncio
import ssl as ssl_module
from asyncio import StreamReader, StreamWriter

from pyleans.net.network import (
    ClientConnectedCallback,
    INetwork,
    NetworkServer,
    SocketInfo,
)


class AsyncioNetwork(INetwork):
    """Production default. Thin pass-through to asyncio."""

    async def start_server(
        self,
        client_connected_cb: ClientConnectedCallback,
        host: str,
        port: int,
        *,
        ssl: ssl_module.SSLContext | None = None,
    ) -> NetworkServer:
        server = await asyncio.start_server(client_connected_cb, host, port, ssl=ssl)
        return _AsyncioNetworkServer(server)

    async def open_connection(
        self,
        host: str,
        port: int,
        *,
        ssl: ssl_module.SSLContext | None = None,
    ) -> tuple[StreamReader, StreamWriter]:
        return await asyncio.open_connection(host, port, ssl=ssl)


class _AsyncioNetworkServer(NetworkServer):
    """NetworkServer wrapping an ``asyncio.Server`` instance."""

    def __init__(self, server: asyncio.Server) -> None:
        self._server = server

    def close(self) -> None:
        self._server.close()

    async def wait_closed(self) -> None:
        await self._server.wait_closed()

    @property
    def sockets(self) -> list[SocketInfo]:
        return list(self._server.sockets)
