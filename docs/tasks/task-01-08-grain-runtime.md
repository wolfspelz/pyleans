# Task 01-08: Grain Runtime (Activation, Scheduling, Idle Collection)

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-03-errors.md](task-01-03-errors.md)
- [task-01-04-serialization.md](task-01-04-serialization.md)
- [task-01-05-provider-abcs.md](task-01-05-provider-abcs.md)
- [task-01-06-grain-decorator.md](task-01-06-grain-decorator.md)
- [task-01-07-grain-base-class.md](task-01-07-grain-base-class.md)

## References
- [adr-concurrency-model](../adr/adr-concurrency-model.md)
- [plan.md](../plan.md) -- Phase 1 items 4 (activation/deactivation), 7 (turn-based scheduler), 12 (idle collection)
- [orleans-grains.md](../orleans-architecture/orleans-grains.md) -- Grain Lifecycle, Single-Threaded Execution
- [orleans-advanced.md](../orleans-architecture/orleans-advanced.md) -- Turn-based concurrency model

## Description

Implement the grain runtime: the core engine that manages grain activations,
enforces turn-based execution, and handles idle collection.

### Files to create
- `src/pyleans/pyleans/server/runtime.py`

### GrainActivation

Represents a live grain instance in memory:

```python
@dataclass
class GrainActivation:
    grain_id: GrainId
    instance: Any                  # the grain object
    inbox: asyncio.Queue           # incoming method calls
    worker_task: asyncio.Task      # the coroutine draining the inbox
    last_activity: float           # monotonic time of last method call
    state_loaded: bool
    etag: str | None               # current storage etag
```

### GrainRuntime

```python
class GrainRuntime:
    def __init__(self, storage_providers: dict[str, StorageProvider],
                 serializer: Serializer, idle_timeout: float = 900.0):
        self._activations: dict[GrainId, GrainActivation] = {}
        self._storage_providers = storage_providers
        self._serializer = serializer
        self._idle_timeout = idle_timeout  # 15 min default

    async def invoke(self, grain_id: GrainId, method_name: str,
                     args: list, kwargs: dict) -> Any:
        """
        Invoke a method on a grain. Activates the grain if needed.
        Enqueues the call and waits for the result (turn-based).
        """

    async def activate_grain(self, grain_id: GrainId) -> GrainActivation:
        """
        Create grain instance, load state from storage, call on_activate.
        Start the worker coroutine.
        """

    async def deactivate_grain(self, grain_id: GrainId) -> None:
        """
        Call on_deactivate, save state, remove from activations.
        """

    async def _grain_worker(self, activation: GrainActivation) -> None:
        """
        Worker loop: drain inbox one message at a time.
        Each message is (method_name, args, kwargs, future).
        Executes the method, sets the future result or exception.
        """

    async def _idle_collector(self) -> None:
        """
        Periodic task that deactivates grains idle longer than threshold.
        """
```

### Turn-based execution

The key invariant: **only one method executes at a time per grain**.

```python
async def _grain_worker(self, activation: GrainActivation):
    while True:
        method_name, args, kwargs, future = await activation.inbox.get()
        activation.last_activity = time.monotonic()
        try:
            method = getattr(activation.instance, method_name)
            result = await method(*args, **kwargs)
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
```

While `await method(...)` is running, the grain may call other grains (which yields
to the event loop), but no other message for THIS grain will execute until the
current one completes.

### State management integration

Grain instances receive two kinds of injected context:

**DI-injected (via constructor, type-hint constructor injection):**
Singleton services like `GrainFactory`, `TimerRegistry`, `SiloManagement`,
`StreamManager`, `Logger`. These are wired by `injector` during
`grain_class()` construction — the runtime does NOT set these.

**Runtime-bound (per-grain-instance, set as attributes during activation):**
`identity`, `state`, `write_state`, `clear_state`. These are unique per
grain activation and cannot be DI singletons.

On activation:
1. Create grain instance (`grain_class()` — DI resolves `` defaults)
2. Set `instance.identity = grain_id` (runtime-bound)
3. If grain has `state_type`: read from storage, deserialize, set `instance.state`
4. Bind `instance.write_state()` and `instance.clear_state()` closures
5. Call `instance.on_activate()` if defined

`instance.write_state()`:
1. Serialize `instance.state` via serializer
2. Call `storage_provider.write(grain_type, key, state_dict, etag)`
3. Update activation's etag

On deactivation:
1. Call `instance.on_deactivate()` if defined
2. Auto-save state if dirty (optional, or leave to user)
3. Cancel worker task
4. Remove from activations dict

### Idle collection

A periodic asyncio task scans all activations every 60 seconds.
If `time.monotonic() - activation.last_activity > idle_timeout`, deactivate.

### Phase 2 extension point (forward reference)

Phase 1 trivially satisfies the single-activation contract from
[adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) because
there is only one silo. Phase 2 preserves the same contract across multiple silos
by introducing two optional collaborators on `GrainRuntime`:

- `directory: IGrainDirectory | None` — the distributed grain directory
  ([task-02-13-distributed-grain-directory.md](task-02-13-distributed-grain-directory.md)).
- `cluster_transport: IClusterTransport | None` — the silo-to-silo mesh
  ([task-02-08-tcp-cluster-transport.md](task-02-08-tcp-cluster-transport.md)).

Both default to `None` in Phase 1. Phase 2 adds a **single routing hook inside
`GrainRuntime.invoke()`** — between args normalization and local activation
lookup — that consults the directory and, if the grain's owner is remote,
forwards the call over `cluster_transport` instead of activating locally. See
[task-02-16-remote-grain-invoke.md](task-02-16-remote-grain-invoke.md) for the
full hook specification. Phase 1 semantics are unchanged when both parameters
are `None`.

### Acceptance criteria

- [x] Grain activated on first call, instance created correctly
- [x] State loaded from storage on activation
- [x] Turn-based: concurrent calls to same grain execute sequentially
- [x] Concurrent calls to different grains execute concurrently
- [x] `on_activate` called after state load
- [x] `on_deactivate` called before deactivation
- [x] `write_state()` persists via storage provider
- [x] Idle grains deactivated after timeout
- [x] Errors in grain methods propagated to caller via future
- [x] Unit tests with mock storage provider

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Files created
- `src/pyleans/pyleans/server/runtime.py` — GrainRuntime, GrainActivation, worker loop, idle collector
- `src/pyleans/test/test_runtime.py` — 20 tests

### Key decisions
- Worker loop uses a sentinel object to signal shutdown (avoids cancellation races).
- `_idle_collector_single_pass` extracted for deterministic test control.
- `write_state` and `clear_state` are bound as closures on the instance (not class methods) to capture activation context.
- `on_activate` is dispatched through the grain's inbox to maintain turn-based ordering.
- `on_deactivate` is called directly (outside inbox) since the grain is shutting down.
- `grain_factory` parameter allows DI integration (Task 10) without coupling to injector.

### Deviations
- None.

### Phase 2 forward reference
Phase 2 will add optional `directory: IGrainDirectory | None` and
`cluster_transport: IClusterTransport | None` constructor parameters and a
single routing hook inside `invoke()` (see
[adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) and
[task-02-16-remote-grain-invoke.md](task-02-16-remote-grain-invoke.md)).
Phase 1 semantics are preserved: when both parameters are `None`, `invoke()`
behaves exactly as specified here. The hook is additive — no Phase 1 test or
behaviour changes.

### Test coverage
- 20 tests: activation (first call, reuse, unknown type/method), state management (load from storage, defaults, save, clear), lifecycle hooks (on_activate/on_deactivate), turn-based execution (sequential same grain, concurrent different grains), error propagation, idle collection, start/stop.


**Resolved**: Grain Base Class (Grain Base Class) implemented in [task-01-07-grain-base-class.md](task-01-07-grain-base-class.md). Test grains updated to use `Grain[TState]`.