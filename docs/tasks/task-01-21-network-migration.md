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

- [ ] `pyleans.net` package imports cleanly; all public symbols re-exported.
- [ ] `test_asyncio_network.py` passes.
- [ ] `test_memory_network.py` passes (full acceptance matrix from task-01-16 including `drain()` real-backpressure test and failure-injection hooks).
- [ ] `ruff`, `pylint` (10.00/10), `mypy` all clean on the new package and its tests.
- [ ] Existing Phase 1 tests still pass (nothing should have changed for them — this is a sanity check).

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

- [ ] All ~349 existing Phase 1 tests pass **without modification**. Real ephemeral ports still bound via the asyncio-adapter default path — intentional.
- [ ] `silo.gateway_port` reports the OS-assigned port when `gateway_port=0`, identical to pre-change behavior.
- [ ] `ruff`, `pylint` (10.00/10), `mypy` all clean on the modified files.

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

- [ ] `network` fixture added to `conftest.py`.
- [ ] All seven test files migrated to the fixture.
- [ ] Hardcoded `59999` replaced with a virtual unbound address; test still asserts `ConnectionError`.
- [ ] Regression guard added and verified against an intentional violation.
- [ ] Full test suite passes (≥349 tests).
- [ ] `ss -tln` during the test run shows no pytest-owned listening sockets (except any scoped production-path smoke test).
- [ ] One production-path smoke test remains covering `AsyncioNetwork` end-to-end.
- [ ] `ruff`, `pylint` (10.00/10), `mypy` all clean.

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
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
