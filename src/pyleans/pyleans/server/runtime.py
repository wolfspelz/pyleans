"""Grain runtime — activation, turn-based scheduling, idle collection."""

import asyncio
import contextlib
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pyleans.cluster.directory import IGrainDirectory
from pyleans.cluster.directory_cache import DirectoryCache
from pyleans.cluster.placement import PlacementStrategy, PreferLocalPlacement
from pyleans.errors import (
    GrainActivationError,
    GrainMethodError,
    RemoteGrainException,
)
from pyleans.grain import get_grain_class, get_grain_methods
from pyleans.grain_base import _current_grain_id
from pyleans.identity import GrainId, SiloAddress
from pyleans.providers.storage import StorageProvider
from pyleans.serialization import Serializer
from pyleans.server.local_directory import LocalGrainDirectory
from pyleans.server.remote_invoke import (
    GRAIN_CALL_HEADER,
    GrainCallFailure,
    GrainCallRequest,
    GrainCallSuccess,
    decode_request,
    decode_response,
    encode_failure,
    encode_request,
    encode_success,
    format_exception_for_wire,
)
from pyleans.transport.cluster import IClusterTransport
from pyleans.transport.errors import (
    TransportConnectionError,
    TransportTimeoutError,
)
from pyleans.transport.messages import MessageType, TransportMessage

logger = logging.getLogger(__name__)

_IDLE_CHECK_INTERVAL = 60.0
_DEFAULT_IDLE_TIMEOUT = 900.0
_DEFAULT_STORAGE_NAME = "default"
_INBOX_MAX_SIZE = 1000
_WORKER_SHUTDOWN_TIMEOUT = 5.0
_SENTINEL = object()


@dataclass
class _MethodCall:
    """A queued method invocation for a grain."""

    method_name: str
    args: list[Any]
    kwargs: dict[str, Any]
    future: asyncio.Future[Any]


@dataclass
class GrainActivation:
    """A live grain instance in memory."""

    grain_id: GrainId
    instance: Any
    inbox: asyncio.Queue[_MethodCall | object] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_INBOX_MAX_SIZE)
    )
    worker_task: asyncio.Task[None] | None = None
    last_activity: float = field(default_factory=time.monotonic)
    state_loaded: bool = False
    etag: str | None = None


_DEFAULT_LOCAL_SILO = SiloAddress(host="127.0.0.1", port=0, epoch=0)


class GrainRuntime:
    """Core engine managing grain activations and turn-based execution."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        storage_providers: dict[str, StorageProvider],
        serializer: Serializer,
        grain_factory: Any = None,
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
        *,
        directory: IGrainDirectory | None = None,
        local_silo: SiloAddress | None = None,
        placement_strategy: PlacementStrategy | None = None,
        transport: IClusterTransport | None = None,
        remote_call_timeout: float = 30.0,
    ) -> None:
        self._activations: dict[GrainId, GrainActivation] = {}
        self._storage_providers = storage_providers
        self._serializer = serializer
        self._grain_factory = grain_factory
        self._idle_timeout = idle_timeout
        self._idle_collector_task: asyncio.Task[None] | None = None
        self._local_silo = local_silo or _DEFAULT_LOCAL_SILO
        self._placement: PlacementStrategy = placement_strategy or PreferLocalPlacement()
        self._directory: IGrainDirectory = directory or LocalGrainDirectory(self._local_silo)
        self._transport = transport
        self._remote_call_timeout = remote_call_timeout
        self._next_call_id_value = 0

    @property
    def local_silo(self) -> SiloAddress:
        return self._local_silo

    @property
    def directory(self) -> IGrainDirectory:
        return self._directory

    @property
    def activations(self) -> dict[GrainId, GrainActivation]:
        """Read-only access to current activations."""
        return self._activations

    async def start(self) -> None:
        """Start the idle collector background task."""
        self._idle_collector_task = asyncio.create_task(self._idle_collector())
        logger.info("Grain runtime started")

    async def stop(self) -> None:
        """Stop the runtime, deactivating all grains."""
        logger.info("Grain runtime stopping, deactivating %d grains", len(self._activations))
        if self._idle_collector_task is not None:
            self._idle_collector_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_collector_task
            self._idle_collector_task = None

        grain_ids = list(self._activations.keys())
        for grain_id in grain_ids:
            await self.deactivate_grain(grain_id)

    async def invoke(
        self,
        grain_id: GrainId,
        method_name: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Invoke a method on a grain, activating it if needed.

        Enqueues the call and waits for the result (turn-based).
        """
        args = args or []
        kwargs = kwargs or {}

        grain_logger = logging.getLogger(f"pyleans.grain.{grain_id.grain_type}")
        grain_logger.debug("Invoking %s.%s on %s", grain_id.grain_type, method_name, grain_id.key)

        entry = await self._directory.resolve_or_activate(
            grain_id,
            self._placement,
            self._local_silo,
        )
        if entry.silo == self._local_silo:
            return await self._invoke_local(grain_id, method_name, args, kwargs)
        try:
            return await self._invoke_remote(entry.silo, grain_id, method_name, args, kwargs)
        except TransportConnectionError:
            self._invalidate_cached_entry(grain_id)
            retry_entry = await self._directory.resolve_or_activate(
                grain_id, self._placement, self._local_silo
            )
            if retry_entry.silo == self._local_silo:
                return await self._invoke_local(grain_id, method_name, args, kwargs)
            return await self._invoke_remote(retry_entry.silo, grain_id, method_name, args, kwargs)

    async def _invoke_local(
        self,
        grain_id: GrainId,
        method_name: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        activation = self._activations.get(grain_id)
        if activation is None:
            activation = await self.activate_grain(grain_id)
        grain_class = get_grain_class(grain_id.grain_type)
        methods = get_grain_methods(grain_class)
        if method_name not in methods:
            raise GrainMethodError(f"Method {method_name!r} not found on {grain_id.grain_type}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        call = _MethodCall(
            method_name=method_name,
            args=args,
            kwargs=kwargs,
            future=future,
        )
        await activation.inbox.put(call)
        return await future

    async def _invoke_remote(
        self,
        owner: SiloAddress,
        grain_id: GrainId,
        method_name: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        if self._transport is None:
            raise RuntimeError(
                f"No cluster transport configured; cannot invoke {grain_id} on {owner.silo_id}",
            )
        self._next_call_id_value += 1
        deadline = None
        if self._remote_call_timeout > 0:
            deadline = time.time() + self._remote_call_timeout
        body = encode_request(
            GrainCallRequest(
                grain_id=grain_id,
                method=method_name,
                args=list(args),
                kwargs=dict(kwargs),
                caller_silo=self._local_silo.silo_id,
                call_id=self._next_call_id_value,
                deadline=deadline,
            )
        )
        try:
            _, resp_body = await self._transport.send_request(
                owner, GRAIN_CALL_HEADER, body, timeout=self._remote_call_timeout
            )
        except TransportTimeoutError as exc:
            raise TimeoutError(
                f"Remote grain call to {grain_id} on {owner.silo_id} timed out"
            ) from exc
        response = decode_response(resp_body)
        if isinstance(response, GrainCallSuccess):
            return response.result
        failure: GrainCallFailure = response
        raise RemoteGrainException(
            exception_type=failure.exception_type,
            message=failure.message,
            remote_traceback=failure.remote_traceback,
        )

    def _invalidate_cached_entry(self, grain_id: GrainId) -> None:
        if isinstance(self._directory, DirectoryCache):
            self._directory.invalidate(grain_id)

    async def handle_grain_call(
        self, source: SiloAddress, msg: TransportMessage
    ) -> TransportMessage | None:
        """Inbound grain-call handler; wire into the transport's dispatcher."""
        del source
        if msg.header != GRAIN_CALL_HEADER or msg.message_type != MessageType.REQUEST:
            return None
        try:
            req = decode_request(msg.body)
        except ValueError as exc:
            logger.warning("Malformed grain-call body: %s", exc)
            body = encode_failure("ValueError", f"malformed body: {exc}", None)
            return TransportMessage(
                message_type=MessageType.RESPONSE,
                correlation_id=msg.correlation_id,
                header=GRAIN_CALL_HEADER,
                body=body,
            )
        if req.deadline is not None and time.time() >= req.deadline:
            logger.info(
                "Dropping grain-call for %s.%s: deadline already expired",
                req.grain_id.grain_type,
                req.method,
            )
            body = encode_failure("TimeoutError", "deadline expired before handler ran", None)
            return TransportMessage(
                message_type=MessageType.RESPONSE,
                correlation_id=msg.correlation_id,
                header=GRAIN_CALL_HEADER,
                body=body,
            )
        try:
            get_grain_class(req.grain_id.grain_type)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Unknown grain type %r in inbound grain-call", req.grain_id.grain_type)
            exc_type, message, tb = format_exception_for_wire(exc)
            body = encode_failure(exc_type, message, tb)
            return TransportMessage(
                message_type=MessageType.RESPONSE,
                correlation_id=msg.correlation_id,
                header=GRAIN_CALL_HEADER,
                body=body,
            )
        try:
            result = await self._invoke_local(req.grain_id, req.method, req.args, req.kwargs)
            body = encode_success(result)
        except Exception as exc:  # pylint: disable=broad-except
            # Unwrap GrainMethodError wrapper so the original grain-author
            # exception type/message crosses the wire.
            original = exc.__cause__ if isinstance(exc, GrainMethodError) else None
            surfaced = original if original is not None else exc
            exc_type, message, tb = format_exception_for_wire(surfaced)
            body = encode_failure(exc_type, message, tb)
        return TransportMessage(
            message_type=MessageType.RESPONSE,
            correlation_id=msg.correlation_id,
            header=GRAIN_CALL_HEADER,
            body=body,
        )

    async def activate_grain(self, grain_id: GrainId) -> GrainActivation:
        """Create grain instance, load state, call on_activate, start worker."""
        if grain_id in self._activations:
            return self._activations[grain_id]

        grain_class = get_grain_class(grain_id.grain_type)
        grain_logger = logging.getLogger(f"pyleans.grain.{grain_id.grain_type}")

        token = _current_grain_id.set(grain_id)
        try:
            if self._grain_factory is not None:
                instance = self._grain_factory(grain_class)
            else:
                instance = grain_class()
        except Exception as e:
            grain_logger.error("Failed to create instance of %s: %s", grain_id, e)
            raise GrainActivationError(
                f"Failed to create instance of {grain_id.grain_type}: {e}"
            ) from e
        finally:
            _current_grain_id.reset(token)

        instance.identity = grain_id

        activation = GrainActivation(grain_id=grain_id, instance=instance)

        state_type = getattr(grain_class, "_state_type", None)
        storage_name = getattr(grain_class, "_storage_name", _DEFAULT_STORAGE_NAME)

        if state_type is not None:
            storage = self._storage_providers.get(storage_name)
            if storage is not None:
                try:
                    state_dict, etag = await storage.read(grain_id.grain_type, grain_id.key)
                    if state_dict:
                        serialized = self._serializer.serialize(state_dict)
                        instance.state = self._serializer.deserialize(serialized, state_type)
                        grain_logger.debug("State loaded from storage for %s", grain_id)
                    else:
                        instance.state = state_type()
                    activation.etag = etag
                    activation.state_loaded = True
                except Exception as e:
                    grain_logger.error("Failed to load state for %s: %s", grain_id, e)
                    raise GrainActivationError(f"Failed to load state for {grain_id}: {e}") from e
            else:
                instance.state = state_type()
                activation.state_loaded = True

            self._bind_state_methods(instance, activation, storage_name, state_type)

        self._bind_deactivate_on_idle(instance, grain_id)

        activation.worker_task = asyncio.create_task(self._grain_worker(activation))
        self._activations[grain_id] = activation

        if hasattr(instance, "on_activate"):
            loop = asyncio.get_running_loop()
            future: asyncio.Future[Any] = loop.create_future()
            call = _MethodCall(method_name="on_activate", args=[], kwargs={}, future=future)
            await activation.inbox.put(call)
            await future

        grain_logger.info("Grain activated: %s", grain_id)
        return activation

    def _bind_state_methods(
        self,
        instance: Any,
        activation: GrainActivation,
        storage_name: str,
        state_type: type,
    ) -> None:
        """Bind read_state, write_state, and clear_state methods to the grain instance."""
        runtime = self
        grain_logger = logging.getLogger(f"pyleans.grain.{activation.grain_id.grain_type}")

        async def read_state() -> None:
            storage = runtime._storage_providers.get(storage_name)
            if storage is None:
                return
            state_dict, etag = await storage.read(
                activation.grain_id.grain_type,
                activation.grain_id.key,
            )
            if state_dict:
                serialized = runtime._serializer.serialize(state_dict)
                instance.state = runtime._serializer.deserialize(serialized, state_type)
            else:
                instance.state = state_type()
            activation.etag = etag
            grain_logger.debug("read_state for %s", activation.grain_id)

        async def write_state() -> None:
            storage = runtime._storage_providers.get(storage_name)
            if storage is None:
                return
            state_dict = dataclasses.asdict(instance.state)
            new_etag = await storage.write(
                activation.grain_id.grain_type,
                activation.grain_id.key,
                state_dict,
                activation.etag,
            )
            activation.etag = new_etag
            grain_logger.debug("write_state for %s", activation.grain_id)

        async def clear_state() -> None:
            storage = runtime._storage_providers.get(storage_name)
            if storage is None:
                return
            await storage.clear(
                activation.grain_id.grain_type,
                activation.grain_id.key,
                activation.etag,
            )
            activation.etag = None
            instance.state = state_type()
            grain_logger.debug("clear_state for %s", activation.grain_id)

        instance.read_state = read_state
        instance.write_state = write_state
        instance.clear_state = clear_state

    def _bind_deactivate_on_idle(self, instance: Any, grain_id: GrainId) -> None:
        """Bind deactivate_on_idle to the grain instance.

        Schedules deactivation after the current turn completes.
        Matches Orleans' DeactivateOnIdle().
        """
        runtime = self

        def deactivate_on_idle() -> None:
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(runtime.deactivate_grain(grain_id))
            )

        instance.deactivate_on_idle = deactivate_on_idle

    async def deactivate_grain(self, grain_id: GrainId) -> None:
        """Call on_deactivate, stop worker, remove from activations."""
        activation = self._activations.get(grain_id)
        if activation is None:
            return

        grain_logger = logging.getLogger(f"pyleans.grain.{grain_id.grain_type}")

        if hasattr(activation.instance, "on_deactivate"):
            try:
                await activation.instance.on_deactivate()
            except Exception as e:
                logger.warning("on_deactivate failed for %s: %s", grain_id, e)

        await activation.inbox.put(_SENTINEL)
        if activation.worker_task is not None:
            try:
                await asyncio.wait_for(activation.worker_task, timeout=_WORKER_SHUTDOWN_TIMEOUT)
            except (TimeoutError, asyncio.CancelledError):
                activation.worker_task.cancel()

        self._activations.pop(grain_id, None)
        await self._directory.unregister(grain_id, self._local_silo)
        grain_logger.info("Grain deactivated: %s", grain_id)

    async def _grain_worker(self, activation: GrainActivation) -> None:
        """Worker loop: drain inbox one message at a time (turn-based)."""
        while True:
            item = await activation.inbox.get()
            if item is _SENTINEL:
                break

            assert isinstance(item, _MethodCall)
            call = item
            activation.last_activity = time.monotonic()

            try:
                method = getattr(activation.instance, call.method_name)
                result = await method(*call.args, **call.kwargs)
                if not call.future.done():
                    call.future.set_result(result)
            except Exception as e:
                logging.getLogger(f"pyleans.grain.{activation.grain_id.grain_type}").warning(
                    "Exception in %s.%s on %s: %s",
                    activation.grain_id.grain_type,
                    call.method_name,
                    activation.grain_id.key,
                    e,
                )
                if not call.future.done():
                    wrapped = GrainMethodError(
                        f"Error in {activation.grain_id.grain_type}.{call.method_name}: {e}"
                    )
                    wrapped.__cause__ = e
                    call.future.set_exception(wrapped)

    async def _idle_collector_single_pass(self) -> None:
        """Run one pass of idle collection. Used by tests and the periodic loop."""
        logger.debug("Idle collection pass, %d active grains", len(self._activations))
        now = time.monotonic()
        to_deactivate = [
            gid
            for gid, act in self._activations.items()
            if now - act.last_activity > self._idle_timeout
        ]
        for grain_id in to_deactivate:
            logger.info("Idle-collecting grain %s", grain_id)
            await self.deactivate_grain(grain_id)

    async def _idle_collector(self) -> None:
        """Periodic task that deactivates grains idle longer than threshold."""
        while True:
            await asyncio.sleep(_IDLE_CHECK_INTERVAL)
            await self._idle_collector_single_pass()
