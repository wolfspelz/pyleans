"""Private I/O helper shared between wire and handshake codecs.

Internal: not part of the public transport API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def readexactly_or_raise(
    reader: asyncio.StreamReader,
    count: int,
    make_error: Callable[[asyncio.IncompleteReadError, int], Exception],
) -> bytes:
    """Read ``count`` bytes or raise a codec-specific error.

    ``make_error`` converts :class:`asyncio.IncompleteReadError` into the
    error the calling codec wants to surface — for example, the wire
    codec maps truncation to :class:`TransportConnectionError`, while
    the handshake codec maps it to :class:`HandshakeError`.
    """
    if count == 0:
        return b""
    try:
        return await reader.readexactly(count)
    except asyncio.IncompleteReadError as exc:
        raise make_error(exc, count) from exc
