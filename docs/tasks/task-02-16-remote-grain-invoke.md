# Task 02-16: Remote Grain Invocation -- Routing, Serialization, Error Propagation

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-08-tcp-cluster-transport.md](task-02-08-tcp-cluster-transport.md)
- [task-02-13-distributed-grain-directory.md](task-02-13-distributed-grain-directory.md)
- [task-02-14-directory-cache.md](task-02-14-directory-cache.md)

## References
- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) -- this task installs the **one routing hook** inside `GrainRuntime.invoke()` through which the single-activation contract becomes observable to every grain caller (gateway, grain-to-grain, in-process). Silo transparency is a structural consequence of placing the hook here.
- [adr-cluster-access-boundary](../adr/adr-cluster-access-boundary.md) -- gateway is the sole external entry; it already dispatches every request through `runtime.invoke()`, so the hook from this task makes *every* gateway silo-transparent with no gateway-side change.
- [orleans-networking.md](../orleans-architecture/orleans-networking.md) -- §4 RPC, §5 message format, §9 correlation, §10 one-way
- [adr-concurrency-model](../adr/adr-concurrency-model.md)

## Description

Up to this point every grain call in Phase 1 has been local: `GrainRuntime.invoke(grain_id, method, args, kwargs)` enqueues a message into the grain's inbox and awaits the future. Phase 2 extends this so the same API transparently routes over the network when the directory says the grain lives on another silo.

The guarantee we deliver: **from grain code, a call to another grain reference looks identical whether the target is local or remote**. Same method signature, same exception propagation, same cancellation behavior. That property is the whole point of the Virtual Actor abstraction.

**Gateway transparency comes for free.** The Phase 1 gateway listener's `_dispatch` already hands every inbound request to `runtime.invoke()`. Adding the directory-lookup + remote-forwarding hook *inside* `invoke()` means client calls through silo A's gateway for a grain owned by silo B are forwarded automatically, with no gateway-side code change. No separate "gateway routing" task is needed — this is the load-bearing reason [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) places the hook in the runtime rather than the gateway.

**Storage stays cluster-oblivious.** Because only the owner silo runs `_invoke_local`, only the owner writes to storage for a given `GrainId`. The storage provider (file, PostgreSQL, …) needs no cluster-membership awareness; per-call etag-CAS is sufficient.

### Files to create/modify

**Create:**
- `src/pyleans/pyleans/server/remote_invoke.py` -- grain-call RPC encoder/decoder, local-vs-remote dispatch helper

**Modify:**
- `src/pyleans/pyleans/server/runtime.py` -- `GrainRuntime.invoke` routes through directory + transport
- `src/pyleans/pyleans/reference.py` -- `GrainRef.__getattr__` unchanged; but proxies learn about remote errors
- `src/pyleans/pyleans/errors.py` -- add `GrainCallError`, `RemoteGrainException`

### Wire-level grain call

```
REQUEST body = orjson-serialized:
{
    "v": 1,                          # schema version
    "grain_type": "CounterGrain",
    "grain_key": "my-counter",
    "method": "increment",
    "args": [...],                   # method positional args
    "kwargs": {...},                 # method keyword args
    "caller": "10.0.0.5:11111:1713441000",
    "call_id": 42,                   # per-caller sequence for observability
    "deadline": 1713441015.250,      # unix seconds; end of the caller's timeout window
}
```

Serialization uses the existing `JsonSerializer`. This keeps Phase 2 protocol-compatible with what the gateway already sends/receives and avoids introducing a new codec under pressure. A binary tagged codec like the Orleans native serializer is explicitly out of scope -- file a Phase 3+ ticket if we need it.

Response body:

```
Success:  {"ok": true,  "result": <serialized return value>}
Failure:  {"ok": false, "exception_type": "ValueError",
           "message": "...", "traceback": "..."}
```

On the caller side, a failure response raises `RemoteGrainException("ValueError: ...", remote_traceback=...)` -- a distinguishable Python exception type with the remote class name and stringified traceback preserved for logs. We do NOT attempt to instantiate the remote exception class on the caller: that opens a pickle-style security hole, and the caller may not have the class imported anyway.

### Routing inside `GrainRuntime.invoke`

```python
async def invoke(self, grain_id, method, args, kwargs) -> Any:
    entry = await self._directory.resolve_or_activate(
        grain_id, self._placement_for(grain_id), caller=self._local_silo
    )
    if entry.silo == self._local_silo:
        return await self._invoke_local(grain_id, method, args, kwargs)
    try:
        return await self._invoke_remote(entry, grain_id, method, args, kwargs)
    except TransportConnectionError:
        self._directory_cache.invalidate(grain_id)
        # Retry once — the grain may have moved to another silo.
        entry = await self._directory.resolve_or_activate(
            grain_id, self._placement_for(grain_id), caller=self._local_silo
        )
        if entry.silo == self._local_silo:
            return await self._invoke_local(grain_id, method, args, kwargs)
        return await self._invoke_remote(entry, grain_id, method, args, kwargs)
```

Policy:
- **One retry** on `TransportConnectionError`. Orleans semantics are at-most-once by default; one retry is the minimum to survive a cache-vs-reality mismatch without crossing into at-least-once territory for the grain method itself.
- **No retry** on `TransportTimeoutError` -- matches Orleans default (`CancelRequestOnTimeout`). The caller sees a `TimeoutError` and can decide whether to retry.
- **No retry** on application exceptions -- they are part of the grain's contract.

### Local invocation unchanged

`_invoke_local` is exactly the Phase 1 path (enqueue into activation inbox, await future). No change.

### Remote invocation

```python
async def _invoke_remote(self, entry, grain_id, method, args, kwargs) -> Any:
    body = self._serializer.dumps({
        "v": 1, "grain_type": grain_id.grain_type, "grain_key": grain_id.key,
        "method": method, "args": list(args), "kwargs": dict(kwargs),
        "caller": self._local_silo.silo_id,
        "call_id": self._next_call_id(),
        "deadline": monotonic_to_unix(asyncio.get_running_loop().time() + timeout),
    })
    header = b"grain-call/v1"
    response_header, response_body = await self._transport.send_request(
        entry.silo, header, body, timeout=timeout
    )
    resp = self._serializer.loads(response_body)
    if resp["ok"]:
        return resp["result"]
    raise RemoteGrainException(resp["exception_type"], resp["message"], resp["traceback"])
```

### Handler registration on the receive side

At silo startup, the runtime registers the `grain-call/v1` handler with the transport. The handler:

1. Deserializes the request.
2. Checks `deadline` -- if already past, returns an `ok=false` with a `TimeoutError` marker (the caller has already given up; no reason to execute).
3. Dispatches to `_invoke_local(grain_id, method, args, kwargs)`.
4. On success: serializes `{"ok": true, "result": ...}`.
5. On exception: captures the class name, `str(exc)`, and truncated `traceback.format_exc()` into the failure response.

The handler runs inside the transport's per-request task (from [task-02-06](task-02-06-silo-connection.md) read loop). Grain-level concurrency is still single-threaded per-grain because `_invoke_local` enqueues into the grain's own inbox.

### Exception serialization -- why names, not objects

Deserializing exception classes from the network means the receiving silo would need every exception type the caller might raise. That couples grain-author code across silos and makes version skew dangerous.

Instead we send string metadata and raise a single `RemoteGrainException` on the caller. Callers can match on `exception_type` string if they want type-specific handling:

```python
try:
    await counter.increment()
except RemoteGrainException as e:
    if e.exception_type == "ValueError":
        ...
```

This is a minor ergonomic loss vs Orleans (which rebuilds the exception type) but it is a deliberate security-first choice for Python where `pickle`-based rebuild is a frequent CVE vector.

### Security considerations

- Grain method args are JSON-deserialized, not pickled -- no arbitrary code execution via deserialization.
- Message size caps from [task-02-04](task-02-04-transport-abcs.md) `TransportOptions.max_message_size` protect against large-payload abuse.
- Exception messages are truncated to 8 KB in the response to prevent pathological payloads.
- The `grain-type` header is validated against the local grain registry; unknown types return a structured error rather than crashing.

### Cancellation

Deferred. Phase 2 does NOT propagate Python's cancel-scope across silos. If the caller is cancelled, the remote grain continues executing to completion; the caller simply stops waiting. This matches Orleans' pre-CancellationToken-support era and keeps the Phase 2 protocol simpler. Phase 4 adds a CANCEL message type.

### Acceptance criteria

- [ ] Grain call targeting a grain on another silo executes on that silo and returns the result
- [ ] **Gateway silo transparency**: a client call through *any* silo's gateway for a remote grain is forwarded transparently; activation count for that `GrainId` across the cluster is exactly 1 ([adr-single-activation-cluster](../adr/adr-single-activation-cluster.md)).
- [ ] Application exception propagates as `RemoteGrainException` with preserved type name and message
- [ ] `TransportConnectionError` triggers one cache invalidation + retry; the grain call succeeds if the retry finds a valid owner
- [ ] `TransportTimeoutError` propagates as `TimeoutError` (no retry)
- [ ] Deadline-already-expired requests do not invoke the grain method
- [ ] Local-vs-remote dispatch is transparent: same test of `counter.increment()` passes regardless of which silo hosts the counter
- [ ] Integration test: 2-silo cluster, activate grain on A, call from B, call from B again (cache hit path), kill A mid-call, verify caller sees `RemoteGrainException` wrapping the connection loss OR a successful retry once the grain is re-activated on B
- [ ] Unit tests cover serialize/deserialize, deadline handling, exception truncation, unknown grain_type, malformed request body

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
