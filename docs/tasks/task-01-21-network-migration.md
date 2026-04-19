# Task 01-21: Network Migration (Temporary)

> **Temporary migration task.** This task exists because the ADR codifying the `INetwork` port was added after Phase 1 tasks 01-15..01-18 (Silo, Counter Grain, Counter App, Counter Client) had already been implemented. Its job is to transform the existing codebase into the state described by the ideal Phase 1 tasks (01-15, 01-16, 01-17, 01-19, 01-20). **Archive after completion.**

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-01-15-network-port.md](task-01-15-network-port.md) (the ideal spec this migration implements)
- [task-01-16-in-memory-network-simulator.md](task-01-16-in-memory-network-simulator.md) (the ideal spec this migration implements)

## References
- [adr-network-port-for-testability](../adr/adr-network-port-for-testability.md)
- Plan file that drove this migration: `~/.claude/plans/no-differently-i-question-enchanted-goose.md` (session-local)

## Description

The current codebase has already implemented Phase 1 tasks 01-15 through 01-20, but they were written **before** [adr-network-port-for-testability](../adr/adr-network-port-for-testability.md) was accepted. Consequently:

- `GatewayListener` calls `asyncio.start_server` directly.
- `ClusterClient` calls `asyncio.open_connection` directly.
- `Silo` has no `network` parameter.
- Tests across seven test files bind real ephemeral TCP ports via `gateway_port=0`.
- A hardcoded port literal `59999` lives in `test_gateway.py`.

This task is the one-time retroactive code transformation that brings those existing files into the state their (rewritten) task specs describe. After this task completes, the repository is indistinguishable from one where the `INetwork` port had been there from day one — and this migration task file becomes purely archival.

The work is split into three **commit-boundaried phases** inside one task. Each phase leaves the repository in a working state. Commit at each phase boundary so reviewers can inspect progress. If Phase C review proves too large in one sitting, split *Phase C only* into per-test-file sub-tasks; do not split Phase A or B (they are too tightly coupled to the port design).

---

## Phase A — Create the `net/` package (new module, no wiring changes)

Create the package described in [task-01-15](task-01-15-network-port.md) and [task-01-16](task-01-16-in-memory-network-simulator.md). No existing code is modified; the package is unused after this phase.

### Work items

- Create `src/pyleans/pyleans/net/__init__.py` — re-exports `INetwork`, `NetworkServer`, `AsyncioNetwork`, `InMemoryNetwork`.
- Create `src/pyleans/pyleans/net/network.py` — `INetwork` ABC, `NetworkServer` ABC, `_SocketInfo`, `ClientConnectedCallback` type alias (spec in [task-01-15 §Design](task-01-15-network-port.md)).
- Create `src/pyleans/pyleans/net/asyncio_network.py` — `AsyncioNetwork`, `_AsyncioNetworkServer` (spec in [task-01-15 §Design](task-01-15-network-port.md)).
- Create `src/pyleans/pyleans/net/memory_network.py` — `InMemoryNetwork`, `InMemoryServer`, `MemoryStreamWriter`, `_MemorySocketInfo`, `_make_stream_pair` helper (full spec in [task-01-16 §Design](task-01-16-in-memory-network-simulator.md) including bounded-queue backpressure, failure injection hooks, asyncio-parity server lifecycle).
- Create `src/pyleans/test/test_asyncio_network.py` — unit tests per [task-01-15 acceptance criteria](task-01-15-network-port.md#acceptance-criteria).
- Create `src/pyleans/test/test_memory_network.py` — unit tests per [task-01-16 acceptance criteria](task-01-16-in-memory-network-simulator.md#acceptance-criteria).

### Acceptance (Phase A)

- [x] `pyleans.net` package imports cleanly; all public symbols re-exported.
- [x] `test_asyncio_network.py` passes.
- [x] `test_memory_network.py` passes (full acceptance matrix from task-01-16 including `drain()` real-backpressure test and failure-injection hooks).
- [x] `ruff`, `pylint` (10.00/10), `mypy` all clean on the new package and its tests.
- [x] Existing Phase 1 tests still pass (nothing should have changed for them — this is a sanity check).

**Phase A note:** Phase A is fully satisfied by the independent completion of task-01-15 and task-01-16, which land the `net/` package, `AsyncioNetwork`, `InMemoryNetwork`, the adapter unit tests, and the regression-guard scaffolding. No additional work is required for Phase A under 01-21 — it is recorded as complete by reference to those commits.

### Commit boundary A

**Commit message**: `"Phase A: INetwork port and adapters (task-01-21)"`.

---

## Phase B — Thread `network: INetwork` through production code (zero behavior change)

Wire the port into the three existing consumer modules. Default `AsyncioNetwork()` is used everywhere, so every existing test that passes today continues to pass unmodified — they still bind real ephemeral ports via the asyncio adapter.

### Work items

- `src/pyleans/pyleans/gateway/listener.py` — add `network: INetwork | None = None` kwarg to `GatewayListener.__init__`; store as `self._network = network or AsyncioNetwork()`; replace the `asyncio.start_server(...)` call with `self._network.start_server(...)`.
- `src/pyleans/pyleans/client/cluster_client.py` — add `network: INetwork | None = None` kwarg to `ClusterClient.__init__`; store as `self._network = network or AsyncioNetwork()`; replace the `asyncio.open_connection(...)` call with `self._network.open_connection(...)`.
- `src/pyleans/pyleans/server/silo.py` — add `network: INetwork | None = None` kwarg to `Silo.__init__`; store as `self._network = network or AsyncioNetwork()`; pass `network=self._network` to the `GatewayListener` constructor.
- `src/pyleans/pyleans/__init__.py` — add `INetwork`, `AsyncioNetwork`, `InMemoryNetwork`, `NetworkServer` to the public re-exports.

### Acceptance (Phase B)

- [x] All existing Phase 1 tests pass **without modification** (408 tests, includes the new adapter suites). Real ephemeral ports still bound via the asyncio-adapter default path — intentional.
- [x] `silo.gateway_port` reports the OS-assigned port when `gateway_port=0`, identical to pre-change behavior.
- [x] `ruff`, `pylint` (10.00/10), `mypy` all clean on the modified files.

### Commit boundary B

**Commit message**: `"Phase B: route TCP I/O through INetwork, production default (task-01-21)"`.

---

## Phase C — Migrate tests to `InMemoryNetwork`; remove real-socket coupling

Switch every existing test that currently binds a real port to use `InMemoryNetwork` instead. This is the behavior-changing phase — after this commit, the test suite consumes zero OS-level TCP ports.

### Work items

**Fixture:**
- `src/pyleans/test/conftest.py` — add a `network` pytest fixture that yields a fresh `InMemoryNetwork()` per test.

**Test files to migrate** (each `make_silo()` factory and each `ClusterClient` constructor call gains the `network` parameter from the fixture):

- `src/pyleans/test/test_gateway.py`
- `src/pyleans/test/test_silo.py`
- `src/pyleans/test/test_silo_management.py`
- `src/pyleans/test/test_string_cache_grain.py`
- `src/counter_app/test/test_counter_app.py`
- `src/counter_app/test/test_counter_grain.py`
- `src/counter_client/test/test_counter_client.py`

**Specific adjustments:**
- Hardcoded port `59999` in `test_gateway.py` (`test_connect_to_nonexistent_gateway_raises`): replace the literal with any unbound virtual address. `InMemoryNetwork.open_connection` against it raises `ConnectionRefusedError` naturally; the test's existing assertion on `ConnectionError` still holds (since `ConnectionRefusedError` is an `OSError` → `ConnectionError` subclass).
- Any sprawling `gateway_port=0` comments or workarounds can be removed — the simulator assigns virtual ports automatically.

**Regression guard:**
- Add a pytest plugin hook (e.g. in `src/pyleans/test/conftest.py`) or a ruff rule that fails test-module collection if the module imports `asyncio.start_server` or `asyncio.open_connection` directly. Message should point at `INetwork`.

**Production-path smoke test (mandatory to keep):**
- Keep at least one existing test (or add one in `test_asyncio_network.py`) that exercises `AsyncioNetwork` end-to-end against a real ephemeral port. Ensures the production code path stays covered and flagging the regression guard as "only applies to test modules that exercise application code" is unambiguous.

### Verification

- Run `pytest` and observe: all ~349 tests pass.
- While `pytest` is running, from another shell: `ss -tln | grep python` (or `lsof -iTCP -sTCP:LISTEN -P -n | grep pytest`). Expect **no output** except any listener explicitly bound by the smoke test (scope it narrowly, one port at a time). Document the command used and the output in the task's Summary.
- Intentionally add `import asyncio; asyncio.start_server` to a throwaway test module; confirm the regression guard fails collection with a clear message; revert the experiment.

### Acceptance (Phase C)

- [x] `network` fixture added to `conftest.py` (at the repo root so all three testpaths pick it up without a `conftest` module-name collision).
- [x] All seven test files migrated to the fixture.
- [x] Hardcoded `59999` repurposed as a *virtual* unbound address (constant `_UNBOUND_GATEWAY`); test still asserts `ConnectionError`/`ConnectionRefusedError`.
- [x] Regression guard added and verified against an intentional violation (the canary `test_regression_guard_fails_collection_for_bad_test_module` spawns a subprocess pytest run and asserts the collection failure).
- [x] Full test suite passes (408 tests, ~1.7s).
- [x] `ss -tln` during the test run shows no pytest-owned listening sockets when `test_asyncio_network.py` is excluded. When included, only that file's adapter smoke tests briefly bind ephemeral ports — as intended.
- [x] One production-path smoke test remains: `src/pyleans/test/test_asyncio_network.py` (12 tests) exercises `AsyncioNetwork` end-to-end.
- [x] `ruff`, `pylint` (10.00/10), `mypy` all clean.

### Commit boundary C

**Commit message**: `"Phase C: migrate tests to InMemoryNetwork, zero OS ports in test suite (task-01-21)"`.

---

## Split decision

**One task, three commits.** Phases A and B are small (new package + three files with ~20 LOC each of production-code edits). Phase C is larger (~150 LOC of test migration + regression guard) but mechanical. Committing at phase boundaries gives reviewers the granularity of three tasks without the coordination overhead.

**Escalation**: if Phase C review exceeds a single sitting, split Phase C *only* into sub-tasks per test file (seven migration sub-tasks + one regression guard sub-task). Do not split Phase A or B.

## What this task does NOT change

- Wire protocol — the gateway protocol framing is unchanged.
- Production defaults — after this task, running `python -m src.counter_app` still binds a real TCP port on `localhost:30000` exactly as before.
- Task-file layout — the renumber and rename that reshapes Phase 1 into its ideal sequence is a documentation change performed separately (in the same planning session) and not part of this task's Phase A/B/C work.

## Findings of code review

No issues found. Review checked:

- Clean code: the Phase B changes are surgical — a single `network: INetwork | None = None` kwarg added to three consumers, each storing `self._network = network or AsyncioNetwork()` and calling through the port. No incidental refactors crept in.
- SOLID: the `INetwork` dependency is injected at each consumer constructor; consumers remain unaware of whether they run against the real stack or the simulator.
- DRY: the `network` fixture lives in exactly one place (root `conftest.py`) and is picked up by all three testpaths. An earlier attempt at per-directory `conftest.py` files was rolled back because pytest's `conftest` module-name collision across sibling dirs broke pre-existing `from conftest import FakeStorageProvider` imports.
- Tests: the seven migrated test files now exercise the runtime end-to-end in-memory; two simulator tests (`test_simulate_reset_unblocks_pending_drain`, `test_drain_returns_immediately_below_high_water`) use an `asyncio.Event`-based release pattern so `server.wait_closed()` never waits 10 seconds for a stale handler.
- Quality gates pass: ruff clean, pylint 10.00/10, mypy clean (the single `yaml` import-untyped warning is pre-existing and environmental, unrelated to this task).

## Findings of security review

No issues found. Review checked:

- No new system-boundary inputs in production code. The test suite now avoids real sockets entirely for application-code coverage, leaving only `AsyncioNetwork`'s own smoke tests binding OS ephemeral ports briefly.
- No new path traversal, injection, or deserialization surfaces.
- The regression guard scans test modules via AST (not substring matching), so crafted docstrings or strings cannot bypass or falsely trip it.
- The canary test spawns a pytest subprocess using `sys.executable` with a controlled argv — no shell, no user-controlled path.
- No unbounded resource consumption: each test gets its own `InMemoryNetwork` instance, which is garbage-collected at fixture teardown; the `Silo` and `ClusterClient` close paths are unchanged by Phase B.

## Summary of implementation

### Files created/modified

**Phase A** (absorbed by task-01-15 and task-01-16 commits):
- Created the `pyleans.net` package, the `AsyncioNetwork` adapter, the `InMemoryNetwork` simulator, their unit-test suites, and the regression-guard plumbing.

**Phase B** (commit `"Task 01-21 Phase B: route TCP I/O through INetwork, production default"`):
- **Modified**: `src/pyleans/pyleans/gateway/listener.py` — added `network: INetwork | None = None` kwarg; replaced `asyncio.start_server` with `self._network.start_server`; `self._server` type changed to `NetworkServer | None`.
- **Modified**: `src/pyleans/pyleans/client/cluster_client.py` — added `network: INetwork | None = None` kwarg; replaced `asyncio.open_connection` with `self._network.open_connection`.
- **Modified**: `src/pyleans/pyleans/server/silo.py` — added `network: INetwork | None = None` kwarg; passed `network=self._network` through to the constructed `GatewayListener`.

**Phase C** (this commit):
- **Created**: `conftest.py` at the repo root — holds the `network` fixture so all three testpaths share it without a `conftest` module-name collision.
- **Modified**: `src/pyleans/test/conftest.py` — regression-guard scaffolding only (the `network` fixture moved to the root conftest); `FakeStorageProvider` stays here for the pyleans tests.
- **Modified**: `src/counter_client/main.py` — `run()` accepts an optional `network: INetwork | None` kwarg so tests can pass `InMemoryNetwork`; the `main()` CLI entry point is unchanged.
- **Migrated** (seven test files): `src/pyleans/test/test_gateway.py`, `test_silo.py`, `test_silo_management.py`, `test_string_cache_grain.py`, `src/counter_app/test/test_counter_app.py`, `test_counter_grain.py`, `src/counter_client/test/test_counter_client.py`. Each `make_silo()` factory now takes a `network: InMemoryNetwork` parameter; every test function has `network: InMemoryNetwork` in its signature and passes it to both the silo and the `ClusterClient`. Hardcoded `localhost:59999` is retained in two files as the constant `_UNBOUND_GATEWAY` — it is now an *unbound virtual address*, not an OS port, and `InMemoryNetwork.open_connection` raises `ConnectionRefusedError` for it naturally.
- **Updated** (two simulator tests): `test_simulate_reset_unblocks_pending_drain` and `test_drain_returns_immediately_below_high_water` switched from `await asyncio.sleep(10)` in their handlers to an `asyncio.Event`-based release so teardown is deterministic and fast.

### Key implementation decisions

- **Root-level `conftest.py` for the `network` fixture.** Initial attempt put per-directory conftests in `src/counter_app/test/` and `src/counter_client/test/`, but pytest loads all `conftest.py` files under the same `conftest` module name and the last one wins in `sys.modules` for other tests' `from conftest import FakeStorageProvider`. Moving the shared fixture to the repo root resolves the collision cleanly.
- **Hardcoded `59999` retained.** The spec allowed replacing the literal with any unbound virtual address. Keeping the existing literal (now under `_UNBOUND_GATEWAY`) preserves test intent ("some address that's not bound") without churn; the simulator's `ConnectionRefusedError` semantics make the assertion still valid.
- **`run()` in `counter_client/main.py` gains an optional `network` kwarg.** This is the only production-code change in Phase C and it's strictly additive — `main()` still calls `run(args)` unchanged, so CLI users see no difference.
- **Handler cleanup via `asyncio.Event`.** `InMemoryServer.wait_closed()` awaits in-flight handlers to completion (asyncio-parity, per spec), so tests whose handlers block indefinitely must signal them before teardown. The `release = asyncio.Event()` pattern is explicit and matches the style used elsewhere in the simulator test file.

### Deviations from original design

- Phase C's `conftest.py` placement moved from `src/pyleans/test/conftest.py` (per the spec) to the repo root, for the reason above. The spec allowed "a pytest plugin hook (e.g. in ...)" — using the root was within the intent.
- No other deviations.

### Test coverage summary

- Full suite: **408 tests, 1.7s** (down from ~21s pre-migration on this branch, and the original pre-port suite was also ~1s range — the simulator is actually faster than real TCP).
- No pytest-owned listening sockets during the run (verified via `ss -tln` sampling) when `test_asyncio_network.py` is excluded. When included, only that one file briefly binds ephemeral ports — the intentional production-path smoke test.
- Canary: a throwaway test module that calls `asyncio.start_server` triggers pytest-collection failure with a message pointing at `INetwork` (verified via subprocess-based `test_regression_guard_fails_collection_for_bad_test_module`).
