---
status: accepted
date: 2026-04-18
tags: [lifecycle, signals, shutdown]
---

# Graceful Shutdown on Catchable Termination Signals

## Context and Problem Statement

A silo that exits without cleanup leaves stale `ACTIVE` entries in the membership table (other silos keep routing to a dead node) and unsaved grain state. Orleans hosts install signal handlers to cover `Ctrl+C`, `kill`, systemd stop, and k8s pod eviction. Python needs the same, across Unix and Windows.

## Decision

The silo cleans up on every catchable termination signal: deactivate all grains (calling `on_deactivate`, persisting state), unregister from the membership table, and close the gateway.

**Shutdown sequence** (`silo.stop()`):

1. Update membership status to `SHUTTING_DOWN`.
2. Cancel the heartbeat background task.
3. Stop the gateway (no new client connections).
4. Stop the runtime (deactivate all grains — calls `on_deactivate`, saves state).
5. Unregister from the membership table (remove entry entirely).
6. Signal the stop event (unblocks `silo.start()`).

**Signal handling**:

The silo installs handlers for all catchable termination signals.

- Unix: `SIGINT` (Ctrl+C) and `SIGTERM` (kill, systemd stop, k8s pod eviction).
- Windows: `SIGINT` (Ctrl+C) via `signal.signal()`, since `add_signal_handler` is not supported on `ProactorEventLoop`.

`SIGKILL` (kill -9) cannot be caught by any process — this is an OS guarantee. If a silo is killed with SIGKILL, its membership entry becomes stale. The heartbeat mechanism detects this: other silos (or a monitor) see the heartbeat timestamp stop updating and can mark the silo as `DEAD` after a timeout.

**atexit fallback**: the silo registers an `atexit` handler that performs a synchronous best-effort cleanup (unregister from membership) for cases where the process exits without an explicit `stop()` call (e.g. unhandled exception in non-async code, interpreter shutdown).

## Design Principle

The silo should never leave a stale `ACTIVE` entry in the membership table under normal operation. Only SIGKILL or power failure can cause stale entries, and those are handled by heartbeat expiry.

## Consequences

- All normal shutdown paths result in clean membership state.
- On Windows, signal coverage is narrower (`SIGINT` only). Service-managed deployments should stop the silo via `silo.stop()` from their lifecycle hooks.
- Grains must tolerate `on_deactivate` running during shutdown — storage calls may fail if the provider is also shutting down.

## Related

- [adr-provider-interfaces](adr-provider-interfaces.md) — membership and storage providers touched during shutdown.
