---
status: accepted
date: 2026-04-18
tags: [runtime, concurrency, asyncio]
---

# Turn-Based Concurrency with One `asyncio.Queue` per Grain

## Context and Problem Statement

Orleans guarantees turn-based, single-threaded execution per grain: only one message runs at a time on a given grain, while other grains run concurrently. We need the same semantics in Python without spawning per-grain threads.

## Decision

One silo = one Python process = one asyncio event loop.

- Each grain activation has a dedicated `asyncio.Queue` for incoming messages.
- A worker coroutine drains the queue one message at a time, enforcing turn-based execution.
- While processing, the grain may `await` (yielding to other grains).
- No other message for the same grain runs until the current one completes.
- Other grains on the same silo run concurrently via asyncio.

The GIL gives us the single-threaded guarantee within a process at no cost. Python 3.13+ free-threaded mode (no GIL) is compatible with this design because isolation is per event-loop, not per-GIL.

## Consequences

- One silo pins to one CPU core. Multi-core scaling means running multiple silo processes — the operator starts N silos on different ports (shell, systemd, k8s, …), and they find each other via the membership table.
- Re-entrancy is not supported in the PoC (a grain calling itself through a proxy would deadlock). Matches Orleans' default.
- Grain code never needs thread-safety concerns — only one turn executes at a time.

## Related

- [adr-dev-mode](adr-dev-mode.md)
- [adr-library-vs-cli](adr-library-vs-cli.md)
