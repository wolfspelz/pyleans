---
status: active
tags: [grain, activation, state, storage]
---

# Grains are an active cache

## Definition

A grain activation is the **authoritative, in-memory copy** of a grain's state inside a silo process. Persistent storage is read exactly once — when the activation is created — and is written back only when the grain explicitly asks for it. While the activation is alive, `grain.state` is the source of truth for that grain across the whole cluster; the storage row is a durable snapshot, not a live mirror.

## Invariants

- **Initial load is once per activation.** When a silo activates a grain, the storage provider is consulted once to populate `grain.state`. If no row exists, the state is initialised to its default.
- **No automatic re-read.** Subsequent grain calls mutate and observe `grain.state` in memory only; the storage provider is never consulted again for the lifetime of the activation.
- **Writes are explicit.** State is persisted only when the grain calls `write_state()` (or `clear_state()` to remove the row). There is no write-through or autosave.
- **Single activation per grain ID, cluster-wide.** At any moment there is at most one activation of a given grain in the cluster, so the in-memory state is authoritative globally — not just on the silo that hosts it.
- **External writes to storage are invisible to a live activation.** Changes made to the storage row by other processes (manual edits, another tool, a different service) do not propagate into `grain.state` until the activation deactivates and a later activation reads the row afresh.
- **Reads through a grain reflect in-memory state, from any silo.** A client connected to a silo that does not host the activation reaches it via the cluster; the returned value is the activation's in-memory state, not a storage read.

## Example

Storage starts with `a.value = 1`. The following sequence shows how the activation takes over as the source of truth:

| Step | Action | Activation `state.value` | Storage row `value` |
|-----:|--------|:------------------------:|:-------------------:|
| 1 | Client on silo A: `set a 42` — silo A activates grain `a`, storage is read | 1 → 42 | 1 |
| 2 | Inside `set`, grain calls `write_state()` | 42 | 42 |
| 3 | Client on silo B: `inc a` — request is routed to silo A (existing activation), no re-read | 43 | 42 |
| 4 | Inside `inc`, grain calls `write_state()` | 43 | 43 |
| 5 | Operator edits the storage file by hand to `value = 0` | 43 | 0 |
| 6 | Any client on any silo: `get a` | 43 | 0 |

`get` returns `43`, not `0`: the activation did not re-read storage in step 6, and will not until it deactivates. Only a subsequent activation (after idle deactivation, silo shutdown, or explicit deactivation request) will observe the `0` written out-of-band in step 5.

## Relationships

- **Activation** — the runtime object that holds `grain.state`, the inbox, and the worker task; created once, then reused for every call to that grain ID on that silo.
- **Storage provider** — the driven port through which the initial read and all explicit writes flow. See [adr-provider-interfaces](../adr/adr-provider-interfaces.md).
- **Single-activation invariant** — the cluster-wide uniqueness of an activation is what makes the in-memory copy safely authoritative. See [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md).
- **Turn-based concurrency** — only one message executes against an activation at a time, so no in-memory state races with storage reads/writes. See [adr-concurrency-model](../adr/adr-concurrency-model.md).
- **Deactivation** — the event that ends the cache: next activation will re-read storage.

## In code

- [src/pyleans/pyleans/server/runtime.py](../../src/pyleans/pyleans/server/runtime.py) — `activate_grain` loads state from storage once and binds `write_state` / `clear_state` to the activation's etag.
- [src/pyleans/pyleans/grain_base.py](../../src/pyleans/pyleans/grain_base.py) — `Grain[TState]` exposes `state`, `write_state()`, `clear_state()`; no `read_state()` is offered, by design.
- [src/counter_app/counter_grain.py](../../src/counter_app/counter_grain.py) — reference example of the explicit-write pattern.

## Related

- [adr-state-declaration](../adr/adr-state-declaration.md) — how a grain declares its state type and storage binding.
- [adr-concurrency-model](../adr/adr-concurrency-model.md) — why in-memory state is safe to mutate without re-reading.
- [adr-single-activation-cluster](../adr/adr-single-activation-cluster.md) — why the in-memory state is authoritative beyond a single silo.
