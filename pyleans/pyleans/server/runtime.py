"""Grain runtime — activation, turn-based scheduling, idle collection."""

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pyleans.errors import (
    GrainActivationError,
    GrainDeactivationError,
    GrainMethodError,
    GrainNotFoundError,
)
from pyleans.grain import get_grain_class, get_grain_methods
from pyleans.identity import GrainId
from pyleans.providers.storage import StorageProvider
from pyleans.serialization import Serializer

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


class GrainRuntime:
    """Core engine managing grain activations and turn-based execution."""

    def __init__(
        self,
        storage_providers: dict[str, StorageProvider],
        serializer: Serializer,
        grain_factory: Any = None,
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self._activations: dict[GrainId, GrainActivation] = {}
        self._storage_providers = storage_providers
        self._serializer = serializer
        self._grain_factory = grain_factory
        self._idle_timeout = idle_timeout
        self._idle_collector_task: asyncio.Task[None] | None = None

    @property
    def activations(self) -> dict[GrainId, GrainActivation]:
        """Read-only access to current activations."""
        return self._activations

    async def start(self) -> None:
        """Start the idle collector background task."""
        self._idle_collector_task = asyncio.create_task(self._idle_collector())

    async def stop(self) -> None:
        """Stop the runtime, deactivating all grains."""
        if self._idle_collector_task is not None:
            self._idle_collector_task.cancel()
            try:
                await self._idle_collector_task
            except asyncio.CancelledError:
                pass
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

        activation = self._activations.get(grain_id)
        if activation is None:
            activation = await self.activate_grain(grain_id)

        grain_class = get_grain_class(grain_id.grain_type)
        methods = get_grain_methods(grain_class)
        if method_name not in methods:
            raise GrainMethodError(
                f"Method {method_name!r} not found on {grain_id.grain_type}"
            )

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

    async def activate_grain(self, grain_id: GrainId) -> GrainActivation:
        """Create grain instance, load state, call on_activate, start worker."""
        if grain_id in self._activations:
            return self._activations[grain_id]

        grain_class = get_grain_class(grain_id.grain_type)

        try:
            if self._grain_factory is not None:
                instance = self._grain_factory(grain_class)
            else:
                instance = grain_class()
        except Exception as e:
            raise GrainActivationError(
                f"Failed to create instance of {grain_id.grain_type}: {e}"
            ) from e

        instance.identity = grain_id

        activation = GrainActivation(grain_id=grain_id, instance=instance)

        state_type = getattr(grain_class, "_state_type", None)
        storage_name = getattr(grain_class, "_storage_name", _DEFAULT_STORAGE_NAME)

        if state_type is not None:
            storage = self._storage_providers.get(storage_name)
            if storage is not None:
                try:
                    state_dict, etag = await storage.read(
                        grain_id.grain_type, grain_id.key
                    )
                    if state_dict:
                        serialized = self._serializer.serialize(state_dict)
                        instance.state = self._serializer.deserialize(
                            serialized, state_type
                        )
                    else:
                        instance.state = state_type()
                    activation.etag = etag
                    activation.state_loaded = True
                except Exception as e:
                    raise GrainActivationError(
                        f"Failed to load state for {grain_id}: {e}"
                    ) from e
            else:
                instance.state = state_type()
                activation.state_loaded = True

            self._bind_state_methods(instance, activation, storage_name, state_type)

        activation.worker_task = asyncio.create_task(self._grain_worker(activation))
        self._activations[grain_id] = activation

        if hasattr(instance, "on_activate"):
            loop = asyncio.get_running_loop()
            future: asyncio.Future[Any] = loop.create_future()
            call = _MethodCall(
                method_name="on_activate", args=[], kwargs={}, future=future
            )
            await activation.inbox.put(call)
            await future

        return activation

    def _bind_state_methods(
        self,
        instance: Any,
        activation: GrainActivation,
        storage_name: str,
        state_type: type,
    ) -> None:
        """Bind save_state and clear_state methods to the grain instance."""
        runtime = self

        async def save_state() -> None:
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

        instance.save_state = save_state
        instance.clear_state = clear_state

    async def deactivate_grain(self, grain_id: GrainId) -> None:
        """Call on_deactivate, stop worker, remove from activations."""
        activation = self._activations.get(grain_id)
        if activation is None:
            return

        if hasattr(activation.instance, "on_deactivate"):
            try:
                await activation.instance.on_deactivate()
            except Exception as e:
                logger.warning("on_deactivate failed for %s: %s", grain_id, e)

        await activation.inbox.put(_SENTINEL)
        if activation.worker_task is not None:
            try:
                await asyncio.wait_for(
                    activation.worker_task, timeout=_WORKER_SHUTDOWN_TIMEOUT
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                activation.worker_task.cancel()

        self._activations.pop(grain_id, None)

    async def _grain_worker(self, activation: GrainActivation) -> None:
        """Worker loop: drain inbox one message at a time (turn-based)."""
        while True:
            item = await activation.inbox.get()
            if item is _SENTINEL:
                break

            call: _MethodCall = item  # type: ignore[assignment]
            activation.last_activity = time.monotonic()

            try:
                method = getattr(activation.instance, call.method_name)
                result = await method(*call.args, **call.kwargs)
                if not call.future.done():
                    call.future.set_result(result)
            except Exception as e:
                if not call.future.done():
                    call.future.set_exception(
                        GrainMethodError(
                            f"Error in {activation.grain_id.grain_type}.{call.method_name}: {e}"
                        )
                    )

    async def _idle_collector_single_pass(self) -> None:
        """Run one pass of idle collection. Used by tests and the periodic loop."""
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
