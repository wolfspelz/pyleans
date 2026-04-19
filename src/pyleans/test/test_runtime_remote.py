"""Tests for :class:`GrainRuntime`'s remote-invocation path.

Covers the task-02-16 acceptance criteria that do not require a full
two-silo integration: remote dispatch via a fake transport,
``TransportConnectionError`` triggers cache invalidate + one retry,
``TransportTimeoutError`` propagates as a plain :class:`TimeoutError`,
deadline-already-expired requests short-circuit without invoking the
grain, unknown ``grain_type`` produces a structured failure response,
and application exceptions propagate as :class:`RemoteGrainException`
with type + message preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pyleans.cluster.directory import DirectoryEntry, IGrainDirectory
from pyleans.cluster.directory_cache import DirectoryCache
from pyleans.cluster.placement import PlacementStrategy, PreferLocalPlacement
from pyleans.errors import RemoteGrainException
from pyleans.grain import _grain_registry, grain
from pyleans.grain_base import Grain
from pyleans.identity import GrainId, SiloAddress
from pyleans.serialization import JsonSerializer
from pyleans.server.remote_invoke import (
    GRAIN_CALL_HEADER,
    GrainCallRequest,
    decode_response,
    encode_failure,
    encode_request,
    encode_success,
)
from pyleans.server.runtime import GrainRuntime
from pyleans.transport.cluster import (
    ConnectionCallback,
    DisconnectionCallback,
    IClusterTransport,
    MessageHandler,
)
from pyleans.transport.errors import (
    TransportConnectionError,
    TransportTimeoutError,
)
from pyleans.transport.messages import MessageType, TransportMessage

# ---- Test grain (registered at module load) ---------------------------------


@grain
class RemoteTestGrain(Grain):
    value: int = 0

    async def inc(self, by: int = 1) -> int:
        self.value += by
        return self.value

    async def fail(self) -> None:
        raise ValueError("kaboom")


# ---- Fakes ------------------------------------------------------------------


@dataclass
class RoutingDirectory(IGrainDirectory):
    """Maps grain_ids to a chosen owner; lets tests flip ownership mid-call."""

    local: SiloAddress
    owner_map: dict[GrainId, SiloAddress] = field(default_factory=dict)

    async def register(self, grain_id: GrainId, silo: SiloAddress) -> DirectoryEntry:
        self.owner_map[grain_id] = silo
        return DirectoryEntry(grain_id, silo, activation_epoch=1)

    async def lookup(self, grain_id: GrainId) -> DirectoryEntry | None:
        silo = self.owner_map.get(grain_id)
        if silo is None:
            return None
        return DirectoryEntry(grain_id, silo, activation_epoch=1)

    async def unregister(self, grain_id: GrainId, silo: SiloAddress) -> None:
        if self.owner_map.get(grain_id) == silo:
            del self.owner_map[grain_id]

    async def resolve_or_activate(
        self,
        grain_id: GrainId,
        placement: PlacementStrategy,
        caller: SiloAddress | None,
    ) -> DirectoryEntry:
        del placement, caller
        silo = self.owner_map.get(grain_id, self.local)
        self.owner_map[grain_id] = silo
        return DirectoryEntry(grain_id, silo, activation_epoch=1)


@dataclass
class ReplyingTransport(IClusterTransport):
    """Fakes send_request by pre-populating (silo_id, header) → response or exception."""

    local: SiloAddress
    responses: dict[tuple[str, bytes], bytes] = field(default_factory=dict)
    raise_on: dict[tuple[str, bytes], Exception] = field(default_factory=dict)
    sent: list[tuple[SiloAddress, bytes, bytes]] = field(default_factory=list)

    async def start(
        self,
        local_silo: SiloAddress,
        cluster_id: object,
        message_handler: MessageHandler,
    ) -> None:
        del local_silo, cluster_id, message_handler

    async def stop(self) -> None:
        pass

    async def connect_to_silo(self, silo: SiloAddress) -> None:
        pass

    async def disconnect_from_silo(self, silo: SiloAddress) -> None:
        pass

    async def send_request(
        self,
        target: SiloAddress,
        header: bytes,
        body: bytes,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        del timeout
        self.sent.append((target, header, body))
        key = (target.silo_id, header)
        if key in self.raise_on:
            raise self.raise_on.pop(key)
        resp = self.responses.get(key)
        if resp is None:
            raise TransportConnectionError(f"no canned reply for {target.silo_id}")
        return header, resp

    async def send_one_way(
        self, target: SiloAddress, header: bytes, body: bytes
    ) -> None:
        del target, header, body

    async def send_ping(self, target: SiloAddress, timeout: float = 10.0) -> float:
        del target, timeout
        return 0.001

    def is_connected_to(self, silo: SiloAddress) -> bool:
        return True

    def get_connected_silos(self) -> list[SiloAddress]:
        return []

    @property
    def local_silo(self) -> SiloAddress:
        return self.local

    def on_connection_established(self, callback: ConnectionCallback) -> None:
        pass

    def on_connection_lost(self, callback: DisconnectionCallback) -> None:
        pass


def _local() -> SiloAddress:
    return SiloAddress(host="local", port=11111, epoch=1)


def _remote() -> SiloAddress:
    return SiloAddress(host="remote", port=11111, epoch=1)


def _make_runtime(
    *,
    directory: IGrainDirectory | None = None,
    transport: IClusterTransport | None = None,
    remote_call_timeout: float = 5.0,
) -> GrainRuntime:
    local = _local()
    return GrainRuntime(
        storage_providers={},
        serializer=JsonSerializer(),
        directory=directory,
        local_silo=local,
        placement_strategy=PreferLocalPlacement(),
        transport=transport,
        remote_call_timeout=remote_call_timeout,
    )


# ---- invoke() remote path ---------------------------------------------------


class TestRemoteInvoke:
    async def test_remote_call_succeeds(self) -> None:
        # Arrange
        local, remote = _local(), _remote()
        grain_id = GrainId("RemoteTestGrain", "r1")
        directory = RoutingDirectory(local=local, owner_map={grain_id: remote})
        transport = ReplyingTransport(local=local)
        transport.responses[(remote.silo_id, GRAIN_CALL_HEADER)] = encode_success(7)
        runtime = _make_runtime(directory=directory, transport=transport)

        # Act
        result = await runtime.invoke(grain_id, "inc", args=[7])

        # Assert
        assert result == 7
        assert len(transport.sent) == 1

    async def test_remote_failure_raises_remote_grain_exception(self) -> None:
        # Arrange
        local, remote = _local(), _remote()
        grain_id = GrainId("RemoteTestGrain", "r2")
        directory = RoutingDirectory(local=local, owner_map={grain_id: remote})
        transport = ReplyingTransport(local=local)
        transport.responses[(remote.silo_id, GRAIN_CALL_HEADER)] = encode_failure(
            "ValueError", "bad input", "Traceback..."
        )
        runtime = _make_runtime(directory=directory, transport=transport)

        # Act / Assert
        with pytest.raises(RemoteGrainException) as excinfo:
            await runtime.invoke(grain_id, "inc")
        assert excinfo.value.exception_type == "ValueError"
        assert excinfo.value.message == "bad input"
        assert excinfo.value.remote_traceback == "Traceback..."

    async def test_connection_error_triggers_single_retry(self) -> None:
        # Arrange - remote unreachable once, then re-resolves to a healthy second owner
        local, remote1 = _local(), _remote()
        remote2 = SiloAddress(host="remote2", port=11111, epoch=1)
        grain_id = GrainId("RemoteTestGrain", "r3")
        directory = RoutingDirectory(local=local, owner_map={grain_id: remote1})
        cache = DirectoryCache(directory)
        transport = ReplyingTransport(local=local)
        transport.raise_on[(remote1.silo_id, GRAIN_CALL_HEADER)] = (
            TransportConnectionError("remote1 lost")
        )
        transport.responses[(remote2.silo_id, GRAIN_CALL_HEADER)] = encode_success(42)
        runtime = _make_runtime(directory=cache, transport=transport)

        # First call resolves to remote1 (through cache); on retry, flip to remote2.
        async def flip_owner_on_retry() -> None:
            directory.owner_map[grain_id] = remote2

        # We need the flip to happen between first attempt and retry; patch
        # the directory's resolve_or_activate to run the flip once.
        original_resolve = cache.resolve_or_activate
        call_count = {"n": 0}

        async def resolve_then_flip(
            gid: GrainId, placement: PlacementStrategy, caller: SiloAddress | None
        ) -> DirectoryEntry:
            call_count["n"] += 1
            if call_count["n"] == 2:
                await flip_owner_on_retry()
            return await original_resolve(gid, placement, caller)

        cache.resolve_or_activate = resolve_then_flip  # type: ignore[method-assign]

        # Act
        result = await runtime.invoke(grain_id, "inc", args=[42])

        # Assert
        assert result == 42

    async def test_timeout_propagates_as_timeout_error(self) -> None:
        # Arrange
        local, remote = _local(), _remote()
        grain_id = GrainId("RemoteTestGrain", "r4")
        directory = RoutingDirectory(local=local, owner_map={grain_id: remote})
        transport = ReplyingTransport(local=local)
        transport.raise_on[(remote.silo_id, GRAIN_CALL_HEADER)] = (
            TransportTimeoutError("grain-call timed out")
        )
        runtime = _make_runtime(directory=directory, transport=transport)

        # Act / Assert
        with pytest.raises(TimeoutError):
            await runtime.invoke(grain_id, "inc")


# ---- handle_grain_call inbound ---------------------------------------------


class TestInboundGrainCall:
    async def test_deadline_already_expired_returns_timeout_failure(self) -> None:
        # Arrange
        runtime = _make_runtime()
        req = GrainCallRequest(
            grain_id=GrainId("RemoteTestGrain", "r5"),
            method="inc",
            args=[],
            kwargs={},
            caller_silo="caller:1:1",
            call_id=1,
            deadline=0.0,  # far in the past
        )
        msg = TransportMessage(
            message_type=MessageType.REQUEST,
            correlation_id=1,
            header=GRAIN_CALL_HEADER,
            body=encode_request(req),
        )

        # Act
        resp = await runtime.handle_grain_call(_remote(), msg)

        # Assert
        assert resp is not None
        body = decode_response(resp.body)
        assert not isinstance(body, type(None))
        # Failure with TimeoutError marker
        from pyleans.server.remote_invoke import GrainCallFailure

        assert isinstance(body, GrainCallFailure)
        assert body.exception_type == "TimeoutError"

    async def test_unknown_grain_type_returns_structured_failure(self) -> None:
        # Arrange
        runtime = _make_runtime()
        req = GrainCallRequest(
            grain_id=GrainId("NoSuchGrain", "x"),
            method="m",
            args=[],
            kwargs={},
            caller_silo="caller:1:1",
            call_id=1,
            deadline=None,
        )
        msg = TransportMessage(
            message_type=MessageType.REQUEST,
            correlation_id=2,
            header=GRAIN_CALL_HEADER,
            body=encode_request(req),
        )

        # Act
        resp = await runtime.handle_grain_call(_remote(), msg)

        # Assert
        assert resp is not None
        from pyleans.server.remote_invoke import GrainCallFailure

        body = decode_response(resp.body)
        assert isinstance(body, GrainCallFailure)

    async def test_local_exception_is_wire_encoded(self) -> None:
        # Arrange
        runtime = _make_runtime()
        req = GrainCallRequest(
            grain_id=GrainId("RemoteTestGrain", "r6"),
            method="fail",
            args=[],
            kwargs={},
            caller_silo="caller:1:1",
            call_id=1,
            deadline=None,
        )
        msg = TransportMessage(
            message_type=MessageType.REQUEST,
            correlation_id=3,
            header=GRAIN_CALL_HEADER,
            body=encode_request(req),
        )

        # Act
        resp = await runtime.handle_grain_call(_remote(), msg)

        # Assert
        from pyleans.server.remote_invoke import GrainCallFailure

        assert resp is not None
        body = decode_response(resp.body)
        assert isinstance(body, GrainCallFailure)
        assert body.exception_type == "ValueError"
        assert body.message == "kaboom"

    async def test_malformed_body_returns_structured_failure(self) -> None:
        # Arrange
        runtime = _make_runtime()
        msg = TransportMessage(
            message_type=MessageType.REQUEST,
            correlation_id=4,
            header=GRAIN_CALL_HEADER,
            body=b"\xffnot json",
        )

        # Act
        resp = await runtime.handle_grain_call(_remote(), msg)

        # Assert
        from pyleans.server.remote_invoke import GrainCallFailure

        assert resp is not None
        body = decode_response(resp.body)
        assert isinstance(body, GrainCallFailure)

    async def test_wrong_header_not_handled(self) -> None:
        # Arrange
        runtime = _make_runtime()
        msg = TransportMessage(
            message_type=MessageType.REQUEST,
            correlation_id=5,
            header=b"pyleans/other/v1",
            body=b"",
        )

        # Act
        resp = await runtime.handle_grain_call(_remote(), msg)

        # Assert
        assert resp is None


# ---- Cleanup registry leak between tests (grain_registry is global) --------


@pytest.fixture(autouse=True)
def _ensure_grain_registered():
    """Other test modules clear the grain registry; make sure our grain is in it."""
    _grain_registry["RemoteTestGrain"] = RemoteTestGrain
    yield
