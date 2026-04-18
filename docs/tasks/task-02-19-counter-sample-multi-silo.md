# Task 02-19: Multi-Silo Counter Sample

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-17-counter-app.md](task-01-17-counter-app.md)
- [task-01-18-counter-client.md](task-01-18-counter-client.md)
- [task-02-16-remote-grain-invoke.md](task-02-16-remote-grain-invoke.md)
- [task-02-17-silo-lifecycle-stages.md](task-02-17-silo-lifecycle-stages.md)
- [task-02-18-multi-silo-integration-tests.md](task-02-18-multi-silo-integration-tests.md)

## References
- [adr-dev-mode](../adr/adr-dev-mode.md)
- [plan.md](../plan.md) -- Phase 2 milestone

## Description

Phase 2's milestone is "two silo processes on localhost, a grain call from silo A executes on silo B." The existing counter sample ([src/counter_app](../../src/counter_app)) is single-silo. This task extends it so a developer can hit the milestone with a two-terminal demo:

```bash
# Terminal 1 — first silo
python -m src.counter_app --port 11111 --gateway 30000 \
    --membership ./data/membership.yaml --cluster-id dev

# Terminal 2 — second silo
python -m src.counter_app --port 11112 --gateway 30001 \
    --membership ./data/membership.yaml --cluster-id dev

# Terminal 3 — client (hits whichever gateway)
python -m src.counter_client --gateway localhost:30000 inc my-counter
python -m src.counter_client --gateway localhost:30001 inc my-counter
python -m src.counter_client --gateway localhost:30000 get my-counter  # prints 2
```

The value of this task is **demonstrability**. Integration tests in [task-02-18](task-02-18-multi-silo-integration-tests.md) prove correctness; the sample proves that a person using the library can run a cluster without reading source code.

### Files to modify/create

**Modify:**
- `src/counter_app/__main__.py` -- CLI accepts `--port`, `--gateway`, `--membership`, `--cluster-id`, `--host` (already partly present; add any missing + document)
- `src/counter_app/main.py` -- construct `Silo` with the transport + distributed directory defaults for Phase 2
- `src/counter_client/main.py` -- `--gateway <host:port>` can be repeated; client round-robins across gateways for fault tolerance

**Create:**
- `src/counter_app/README.md` -- two-silo demo walkthrough (small, runnable snippets)
- `docs/adr/adr-counter-sample-scope.md` -- records the sample's educational scope (why the counter stays simple instead of growing production-sample features)

### CLI options

```
--host            HOST      host bind for silo + gateway (default: localhost)
--port            INT       silo-to-silo TCP port (default: 11111; increment for extra silos)
--gateway         INT       gateway TCP port (default: 30000; increment for extra silos)
--membership      PATH      shared YAML file (default: ./data/membership.yaml)
--cluster-id      STRING    cluster identifier (default: dev)
--storage-dir     PATH      storage root (default: ./data/storage)
--log-level       LEVEL     default INFO
```

The defaults match Phase 1 for backwards compat. A developer running a single silo with default flags sees no behavior change.

### Demo walkthrough

The README walks through three exercises, each building on the last:

1. **Two silos, one counter.** Start two silos. Issue `inc` from either gateway. Observe via logs that the counter grain activated on exactly one silo (whichever the directory picked); subsequent calls route there regardless of which gateway the client hit.

2. **Kill a silo, counter survives.** With the counter active on silo 2, Ctrl-C silo 2. Wait ~15 seconds for failure detection. Issue another `inc` from silo 1's gateway. Observe: counter re-activates on silo 1 with state reloaded from disk; value increments correctly.

3. **Restart the dead silo.** Start silo 2 again (new epoch). Issue `inc` from silo 2's gateway. The directory may or may not pick silo 2 for the grain based on its ring position -- either outcome demonstrates placement is deterministic from grain_id hash. Show the log lines that reveal the decision.

README keeps the explanation terse; for theory, it links to `docs/architecture/consistent-hash-ring.md` and `docs/orleans-architecture/orleans-cluster.md`.

### Client gateway fallback

The CLI accepts multiple `--gateway` flags:

```
python -m src.counter_client --gateway localhost:30000 --gateway localhost:30001 inc my-counter
```

`ClusterClient` picks one gateway per call; on connection error, it falls back to the next. No global state needed for Phase 2 -- one-shot CLI invocations start fresh each time. The repeated flag is there so the demo README can show fault tolerance without a live-retry script.

### Avoid scope creep

This is a sample, not a production example. The following are **out of scope**, with rationale committed to the ADR:

- No config file support (CLI flags are enough for the demo).
- No metrics export, no dashboard.
- No authentication on the gateway (see [adr-cluster-transport](../adr/adr-cluster-transport.md) -- TLS is a Phase 4 concern even for the built-in transport).
- No Docker Compose recipe. That belongs in a separate `examples/` tree post-PoC.
- No grain beyond the existing counter + string cache.

Keeping the sample lean means reviewers can read the whole thing in one sitting and be confident the cluster behavior it demonstrates is exactly what the framework delivers.

### Acceptance criteria

- [ ] Running `python -m src.counter_app` with default flags produces the same single-silo behavior as Phase 1
- [ ] Two `counter_app` processes on different ports with shared membership file form a cluster
- [ ] `counter_client` successfully increments a counter via each gateway; values are consistent across both
- [ ] Killing one silo and waiting ~15 s leads to grain re-activation on the surviving silo on next call
- [ ] README demo instructions execute exactly as written on a clean checkout
- [ ] CLI accepts multiple `--gateway` flags and falls back on connection error
- [ ] No tests beyond README-verification needed -- integration tests in [task-02-18](task-02-18-multi-silo-integration-tests.md) cover correctness; this task covers demonstrability

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
