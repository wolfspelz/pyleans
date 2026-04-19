"""Full-mesh manager for silo-to-silo connections.

:class:`SiloConnectionManager` owns every TCP link this silo has to every
other silo in the cluster. It establishes and tears down connections,
performs the handshake, deduplicates simultaneous dials, reconnects with
exponential backoff while peers remain in active membership, and fires
callbacks so the directory cache and failure detector can react to
link-state changes.

See :doc:`../../../../docs/tasks/task-02-07-silo-connection-manager` for
the full design and acceptance criteria.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable

from pyleans.cluster.identity import ClusterId
from pyleans.identity import SiloAddress
from pyleans.transport.cluster import (
    ConnectionCallback,
    DisconnectionCallback,
    MessageHandler,
)
from pyleans.transport.errors import (
    HandshakeError,
    TransportConnectionError,
    TransportError,
)
from pyleans.transport.handshake import (
    PROTOCOL_VERSION,
    Handshake,
    encode_handshake,
    read_handshake,
    validate_peer_handshake,
)
from pyleans.transport.options import TransportOptions
from pyleans.transport.tcp.connection import SiloConnection

logger = logging.getLogger(__name__)

_SleepFn = Callable[[float], Awaitable[None]]
"""Injection seam used by reconnect tests — swapped for a deterministic fake."""


class SiloConnectionManager:
    """Owns every live :class:`SiloConnection` this silo holds."""

    # pylint: disable=too-many-instance-attributes  # Orchestration state is irreducible.

    def __init__(
        self,
        local_silo: SiloAddress,
        cluster_id: ClusterId,
        options: TransportOptions,
        message_handler: MessageHandler,
        *,
        role_flags: int = 0,
        sleep: _SleepFn = asyncio.sleep,
    ) -> None:
        self._local_silo = local_silo
        self._cluster_id = cluster_id
        self._options = options
        self._handler = message_handler
        self._role_flags = role_flags
        self._sleep = sleep
        self._connections: dict[SiloAddress, SiloConnection] = {}
        self._initiators: dict[SiloAddress, SiloAddress] = {}
        self._pending_connects: dict[SiloAddress, asyncio.Task[SiloConnection]] = {}
        self._reconnect_tasks: dict[SiloAddress, asyncio.Task[None]] = {}
        self._monitor_tasks: dict[SiloAddress, asyncio.Task[None]] = {}
        self._active_silos: set[SiloAddress] = set()
        self._on_established: list[ConnectionCallback] = []
        self._on_lost: list[DisconnectionCallback] = []
        self._stopped = False

    @property
    def local_silo(self) -> SiloAddress:
        return self._local_silo

    def on_connection_established(self, callback: ConnectionCallback) -> None:
        """Register a callback fired after a peer connection becomes live."""
        self._on_established.append(callback)

    def on_connection_lost(self, callback: DisconnectionCallback) -> None:
        """Register a callback fired after a peer connection drops."""
        self._on_lost.append(callback)

    def get_connection(self, silo: SiloAddress) -> SiloConnection | None:
        conn = self._connections.get(silo)
        if conn is None or conn.is_closed:
            return None
        return conn

    def get_connected_silos(self) -> list[SiloAddress]:
        return [silo for silo, conn in self._connections.items() if not conn.is_closed]

    async def connect(self, silo: SiloAddress) -> SiloConnection:
        """Return a live connection to ``silo``, dialling if necessary.

        Concurrent calls for the same silo coalesce to one outbound
        dial. Fails with :class:`HandshakeError` or
        :class:`TransportConnectionError` on unrecoverable setup
        failures; the caller is free to retry (or let the reconnect
        loop take over).
        """
        self._raise_if_stopped()
        existing = self.get_connection(silo)
        if existing is not None:
            return existing
        pending = self._pending_connects.get(silo)
        if pending is not None:
            return await pending
        task: asyncio.Task[SiloConnection] = asyncio.get_running_loop().create_task(
            self._do_outbound_connect(silo),
            name=f"silo-mgr-connect:{silo}",
        )
        self._pending_connects[silo] = task
        try:
            return await task
        finally:
            if self._pending_connects.get(silo) is task:
                del self._pending_connects[silo]

    async def accept_inbound(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle an incoming TCP connection: handshake, dedup, install."""
        if self._stopped:
            _close_writer_silently(writer)
            return
        try:
            peer = await read_handshake(reader, self._options.handshake_timeout)
            validate_peer_handshake(peer, self._cluster_id, expected_silo=None)
            local_hs = self._build_handshake()
            writer.write(encode_handshake(local_hs))
            await writer.drain()
        except (HandshakeError, ConnectionError, OSError) as exc:
            logger.warning("inbound handshake failed: %s", exc)
            _close_writer_silently(writer)
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        try:
            remote = SiloAddress.from_silo_id(peer.silo_id)
        except ValueError as exc:
            logger.warning("inbound handshake silo_id unparseable: %s", exc)
            _close_writer_silently(writer)
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        await self._install_connection(reader, writer, remote, initiator=remote)

    async def disconnect(self, silo: SiloAddress) -> None:
        """Close ``silo``'s connection and halt any reconnection attempts."""
        task = self._reconnect_tasks.pop(silo, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._active_silos.discard(silo)
        conn = self._connections.pop(silo, None)
        if conn is not None:
            self._initiators.pop(silo, None)
            await conn.close("normal")
        monitor = self._monitor_tasks.pop(silo, None)
        if monitor is not None and not monitor.done():
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await monitor

    async def update_active_silos(self, active: set[SiloAddress]) -> None:
        """Membership update: open links to new peers, close links to removed ones."""
        active = set(active) - {self._local_silo}
        newly_gone = set(self._active_silos) - active
        newly_added = active - set(self._active_silos)
        self._active_silos = set(active)
        for silo in newly_gone:
            await self.disconnect(silo)
        for silo in newly_added:
            asyncio.get_running_loop().create_task(
                self._eager_connect(silo),
                name=f"silo-mgr-eager:{silo}",
            )

    async def stop(self) -> None:
        """Cancel reconnect/monitor tasks and close every connection.

        Total shutdown budget is :attr:`TransportOptions.shutdown_timeout`;
        any connection still open at the deadline is forcibly closed.
        """
        if self._stopped:
            return
        self._stopped = True
        self._active_silos.clear()
        background_tasks: list[asyncio.Task[object]] = [
            *self._reconnect_tasks.values(),
            *self._monitor_tasks.values(),
            *self._pending_connects.values(),
        ]
        for task in background_tasks:
            if not task.done():
                task.cancel()
        for task in background_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._reconnect_tasks.clear()
        self._monitor_tasks.clear()
        self._pending_connects.clear()

        async def _close_all() -> None:
            await asyncio.gather(
                *(conn.close("normal") for conn in self._connections.values()),
                return_exceptions=True,
            )

        try:
            await asyncio.wait_for(_close_all(), timeout=self._options.shutdown_timeout)
        except TimeoutError:
            logger.warning("shutdown timeout exceeded; forcing remaining closes")
            await asyncio.gather(
                *(conn.close("lost") for conn in self._connections.values()),
                return_exceptions=True,
            )
        self._connections.clear()
        self._initiators.clear()

    async def _do_outbound_connect(self, silo: SiloAddress) -> SiloConnection:
        network = self._options.network
        try:
            open_coro = network.open_connection(silo.host, silo.port, ssl=self._options.ssl_context)
            reader, writer = await asyncio.wait_for(
                open_coro, timeout=self._options.connect_timeout
            )
        except (ConnectionRefusedError, OSError, TimeoutError) as exc:
            raise TransportConnectionError(f"failed to open connection to {silo}: {exc}") from exc
        try:
            local_hs = self._build_handshake()
            writer.write(encode_handshake(local_hs))
            await writer.drain()
            peer = await read_handshake(reader, self._options.handshake_timeout)
            validate_peer_handshake(peer, self._cluster_id, expected_silo=silo)
        except (HandshakeError, ConnectionError, OSError) as exc:
            _close_writer_silently(writer)
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if isinstance(exc, HandshakeError):
                raise
            raise TransportConnectionError(f"handshake I/O failed for {silo}: {exc}") from exc
        return await self._install_connection(reader, writer, silo, initiator=self._local_silo)

    async def _install_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        remote: SiloAddress,
        initiator: SiloAddress,
    ) -> SiloConnection:
        new_conn = SiloConnection(reader, writer, remote, self._options, self._handler)
        existing = self._connections.get(remote)
        if existing is not None and not existing.is_closed:
            winner_is_existing = self._dedup_keeps_existing(
                existing_initiator=self._initiators.get(remote, remote),
                new_initiator=initiator,
            )
            if winner_is_existing:
                logger.info("dedup drops new connection to %s (existing wins)", remote)
                _close_writer_silently(writer)
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                return existing
            logger.info("dedup drops existing connection to %s (new wins)", remote)
            self._connections.pop(remote, None)
            self._initiators.pop(remote, None)
            monitor = self._monitor_tasks.pop(remote, None)
            if monitor is not None and not monitor.done():
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await monitor
            await existing.close("normal")
        self._connections[remote] = new_conn
        self._initiators[remote] = initiator
        await new_conn.start()
        self._monitor_tasks[remote] = asyncio.get_running_loop().create_task(
            self._monitor_connection(remote, new_conn),
            name=f"silo-mgr-monitor:{remote}",
        )
        await self._fire_established(remote)
        return new_conn

    def _dedup_keeps_existing(
        self,
        *,
        existing_initiator: SiloAddress,
        new_initiator: SiloAddress,
    ) -> bool:
        """Return True iff the existing connection wins the dedup race.

        Rule: keep the connection initiated by the silo with the
        lexicographically smaller ``silo_id``. Ties (same initiator id
        on both) favour the existing connection.
        """
        if existing_initiator == new_initiator:
            return True
        return existing_initiator.silo_id < new_initiator.silo_id

    async def _monitor_connection(self, silo: SiloAddress, conn: SiloConnection) -> None:
        reason = await conn.wait_closed()
        if self._connections.get(silo) is not conn:
            # Superseded by a dedup winner or an explicit disconnect.
            return
        self._connections.pop(silo, None)
        self._initiators.pop(silo, None)
        self._monitor_tasks.pop(silo, None)
        cause: Exception | None = (
            TransportConnectionError("connection lost") if reason == "lost" else None
        )
        await self._fire_lost(silo, cause)
        if reason == "lost" and silo in self._active_silos and not self._stopped:
            self._schedule_reconnect(silo)

    def _schedule_reconnect(self, silo: SiloAddress) -> None:
        existing = self._reconnect_tasks.get(silo)
        if existing is not None and not existing.done():
            return
        self._reconnect_tasks[silo] = asyncio.get_running_loop().create_task(
            self._reconnect_loop(silo),
            name=f"silo-mgr-reconnect:{silo}",
        )

    async def _reconnect_loop(self, silo: SiloAddress) -> None:
        attempt = 0
        try:
            while silo in self._active_silos and not self._stopped:
                delay = min(
                    self._options.reconnect_base_delay * (2**attempt),
                    self._options.reconnect_max_delay,
                )
                jitter_fraction = self._options.reconnect_jitter_fraction
                jitter = random.uniform(-delay * jitter_fraction, delay * jitter_fraction)
                await self._sleep(max(0.0, delay + jitter))
                if silo not in self._active_silos or self._stopped:
                    return
                try:
                    await self.connect(silo)
                    return
                except HandshakeError as exc:
                    logger.error(
                        "reconnect to %s aborted (unrecoverable handshake error): %s",
                        silo,
                        exc,
                    )
                    return
                except (TransportConnectionError, TransportError, OSError) as exc:
                    attempt += 1
                    logger.warning("reconnect to %s failed (attempt %d): %s", silo, attempt, exc)
        finally:
            if self._reconnect_tasks.get(silo) is asyncio.current_task():
                self._reconnect_tasks.pop(silo, None)

    async def _eager_connect(self, silo: SiloAddress) -> None:
        try:
            await self.connect(silo)
        except (HandshakeError, TransportConnectionError, TransportError) as exc:
            logger.warning("eager connect to %s failed: %s", silo, exc)
            if silo in self._active_silos and not self._stopped:
                self._schedule_reconnect(silo)

    def _build_handshake(self) -> Handshake:
        return Handshake(
            protocol_version=PROTOCOL_VERSION,
            cluster_id=self._cluster_id.value,
            silo_id=self._local_silo.silo_id,
            role_flags=self._role_flags,
        )

    async def _fire_established(self, silo: SiloAddress) -> None:
        for cb in list(self._on_established):
            await _safely_invoke(cb, silo)

    async def _fire_lost(self, silo: SiloAddress, cause: Exception | None) -> None:
        for cb in list(self._on_lost):
            await _safely_invoke(cb, silo, cause)

    def _raise_if_stopped(self) -> None:
        if self._stopped:
            raise TransportError("connection manager stopped")


def _close_writer_silently(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()


async def _safely_invoke(callback: Callable[..., Awaitable[None]], *args: object) -> None:
    """Invoke a callback, swallowing exceptions so one bad observer can't cascade."""
    try:
        await callback(*args)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("connection manager callback raised")
