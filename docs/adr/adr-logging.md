---
status: accepted
date: 2026-04-18
tags: [observability, logging]
---

# Logging via stdlib `logging` with Per-Grain-Type Loggers

## Context and Problem Statement

pyleans needs a logging strategy that:

- works with any Python logging setup the user already has;
- lets operators filter noise per silo, module, or grain type;
- never hijacks global logging configuration — libraries must not configure handlers.

## Decision

pyleans follows the Python library convention. Every module creates its own logger via `logging.getLogger(__name__)`. Grains get a per-type logger via `logging.getLogger(f"pyleans.grain.{grain_type}")`. The library **never** configures handlers or levels — that is the application's responsibility.

**Logger hierarchy**:

```
pyleans                          ← root, controls everything
pyleans.silo                     ← silo lifecycle (start, stop, membership)
pyleans.runtime                  ← activation, deactivation, idle collection
pyleans.grain.<GrainType>        ← per-grain-type (e.g. pyleans.grain.CounterGrain)
pyleans.storage                  ← storage provider read/write/clear
pyleans.gateway                  ← gateway protocol, client connections
pyleans.membership               ← membership provider operations
pyleans.streaming                ← stream provider publish/subscribe
pyleans.timer                    ← timer registration and tick callbacks
```

The per-grain-type logger (`pyleans.grain.CounterGrain`) enables filtering individual grain types independently. Setting `pyleans.grain` controls all grains at once.

**Log level guideline**: a log line that fires more than once per second per module or per grain instance is DEBUG. Everything else is INFO or above.

| Level | When | Examples |
|---|---|---|
| INFO | Lifecycle events, ≤1/sec | Silo start/stop, grain activation/deactivation, provider init, membership changes |
| DEBUG | Per-operation, frequent | Grain method calls, `write_state`/`clear_state`, storage read/write, gateway messages, timer ticks, idle collection passes |
| WARNING | Unexpected but recoverable | Grain method exception (caught), storage etag conflict, deactivation timeout |
| ERROR | Operation failed | Grain activation failure, provider I/O error, unhandled grain exception |

**Default log levels**:

- Dev mode (single silo): `pyleans` logger at **INFO** — see lifecycle events, quiet on per-call traffic.
- Multi-silo cluster: `pyleans` logger at **WARNING** — only problems surface.

The Silo sets the default level on the `pyleans` root logger during startup **only if no handler is already configured** (following the pattern of `logging.basicConfig` — no-op if the user already configured logging). The user can override any logger at any time with standard Python logging:

```python
import logging

# All grains INFO, CounterGrain DEBUG, AnswerGrain WARNING
logging.getLogger("pyleans.grain").setLevel(logging.INFO)
logging.getLogger("pyleans.grain.CounterGrain").setLevel(logging.DEBUG)
logging.getLogger("pyleans.grain.AnswerGrain").setLevel(logging.WARNING)

# See storage operations
logging.getLogger("pyleans.storage").setLevel(logging.DEBUG)

# Or configure from a file before creating the silo
logging.config.fileConfig("logging.ini")

silo = Silo(grains=[CounterGrain], ...)
await silo.start()
```

No pyleans-specific logging configuration format. Standard Python logging is the configuration mechanism — `dictConfig`, `fileConfig`, or programmatic calls.

## Considered Options

- **Stdlib `logging` with per-grain-type loggers** — chosen. Zero new concepts, integrates with every Python logging stack.
- **Structured logging (structlog / loguru) as a hard dependency** — rejected. Opinionated, would fight existing user setups. Users are free to plug `structlog` into stdlib `logging` if desired.
- **Custom pyleans logging config format** — rejected. Yet-another config format, no benefit over `dictConfig`.

## Consequences

- Zero logging setup required in pyleans itself.
- Users integrate with existing logging stacks (structlog, JSON formatters, ELK) without adapter code.
- Enforcement of the ≤1/sec-per-grain rule is a code-review and CLAUDE.md concern, not a runtime check.

## Related

- [adr-grain-base-class](adr-grain-base-class.md) — `self.logger` property on grains.
