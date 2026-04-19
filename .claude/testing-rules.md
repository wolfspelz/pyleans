# Testing Rules — pyleans

Detailed rules for writing tests. Referenced from [CLAUDE.md](../CLAUDE.md) §Testing Requirements; those rules take precedence if anything here conflicts.

These rules apply to every test in the repository — unit, integration, and end-to-end — unless a specific exception is called out.

---

## 1. Arrange / Act / Assert — mandatory, explicitly labeled

Every test follows the **Arrange → Act → Assert** structure (equivalent to BDD's Given → When → Then). Each phase has a single purpose:

- **Arrange**: set up the preconditions — build inputs, instantiate collaborators, prime fakes.
- **Act**: perform *the one* behavior under test.
- **Assert**: verify the outcome.

### 1.1 Use explicit section-label comments

Every non-trivial test MUST separate its phases with one of:

```python
# Arrange
# Act
# Assert
```

Blank lines alone are not sufficient — the labels are the point. Readers should be able to scan a test and see each phase without parsing code. Example:

```python
async def test_increment_advances_value_by_one(network: InMemoryNetwork) -> None:
    # Arrange
    silo = make_silo(network)
    await silo.start_background()
    counter = silo.grain_factory.get_grain(CounterGrain, "c1")

    # Act
    new_value = await counter.increment()

    # Assert
    assert new_value == 1
    assert await counter.get_value() == 1

    await silo.stop()
```

### 1.2 One Act per test — *not* Arrange-Act-Assert-Act-Assert

A test has **exactly one Act step**. If you find yourself writing a second call to the system under test followed by more assertions, split into two tests. The single exception: when the *behavior being tested* is inherently a sequence (e.g. "increment twice returns 2"), the Act can be a small fixed sequence — but then the assertions still verify the composite outcome, not intermediate state.

Anti-pattern:

```python
# Act
result1 = service.do_thing()
# Assert
assert result1 == expected1
# Act   ← wrong; split this into its own test
result2 = service.do_other_thing()
# Assert
assert result2 == expected2
```

### 1.3 Omit the Arrange label (only) when there's no Arrange

For trivial tests where Arrange is empty or one line, the `# Arrange` comment can be omitted — but `# Act` and `# Assert` remain. Example:

```python
def test_counter_state_default_value_is_zero() -> None:
    # Act
    state = CounterState()

    # Assert
    assert state.value == 0
```

### 1.4 Bind the Act result to a named variable

Start the Act line with `result =` (or a domain-appropriate name) so it's visible at a glance which line is the Act. If the Act returns nothing (e.g. a state-changing call), put the bare call on the Act line and assert on the mutated state in Assert:

```python
# Act
await counter.set_value(42)

# Assert
assert await counter.get_value() == 42
```

### 1.5 Complex Arrange → extract to a fixture

If the Arrange section exceeds ~10 lines or is duplicated across three or more tests, move it to a pytest fixture. See §4 for fixture placement.

---

## 2. Unit vs integration vs end-to-end tests

pyleans distinguishes three test scopes. Every new test declares which it is by its location and what it touches.

### 2.1 Unit test

- Tests **one module or class** in isolation.
- All collaborators crossing a port are **fakes or in-memory test doubles** (not mocks — see §3).
- No filesystem, no real TCP, no real subprocess, no real time (use `freezegun` or parameterize where needed).
- Must complete in **<100 ms** on a developer laptop.

### 2.2 Integration test

- Tests **multiple pyleans components wired together** (e.g. `Silo` + `GatewayListener` + `ClusterClient`) via the real production code paths.
- External systems still use test doubles: in-memory storage, the `InMemoryNetwork` simulator, fake membership, etc.
- Runs on the same pytest invocation as unit tests — no separate marker or directory. Speed budget: **<1 s per test**.

### 2.3 End-to-end / production-path smoke test

- Exercises real OS-level resources the adapter relies on (real TCP stack via `AsyncioNetwork`, real filesystem for `FileStorageProvider`, etc.).
- Lives in a clearly named file (e.g. [`test_asyncio_network.py`](../src/pyleans/test/test_asyncio_network.py)). This is the *only* category allowed to bind OS ports, create scratch directories, or touch the real clock.
- Kept to the minimum needed to prove the production adapter works. Every test here should have a justification in a one-line docstring.

### 2.4 The network is *never* the real OS TCP stack in unit / integration tests

This rule is enforced by the regression guard in [`src/pyleans/test/conftest.py`](../src/pyleans/test/conftest.py) (see [adr-network-port-for-testability](../docs/adr/adr-network-port-for-testability.md)). Tests must use the `network` fixture from the repo-root [`conftest.py`](../conftest.py) and pass it to `Silo(network=network)` and `ClusterClient(network=network)`. The guard fails collection on any test module that imports `asyncio.start_server` or `asyncio.open_connection` directly.

---

## 3. Fakes vs mocks — prefer fakes

- **Fake**: a working in-memory implementation of a port (e.g. `FakeStorageProvider`, `InMemoryNetwork`). It has real behavior; assertions target outputs.
- **Mock**: a call-recorder that pretends to be a collaborator and lets you assert that `foo.bar` was called with specific args.

**Default to fakes.** Mocks are allowed only when:

1. The collaborator has no port (rare in pyleans — most cross-module interaction already goes through ports).
2. You need to assert the exact sequence of interactions, not just the outcome.
3. No fake exists *and* writing one would cost more than the test is worth.

If you reach for a mock, leave a one-line comment explaining why the fake isn't sufficient. "Test the system, not the test double" — if an assertion looks like `mock.foo.assert_called_once_with(...)`, ask whether the outcome itself could be asserted instead.

---

## 4. Fixtures and `conftest.py` placement

- Fixtures shared across **all** testpaths live in the repo-root [`conftest.py`](../conftest.py). This is where `network` lives.
- Fixtures specific to a single testpath live in that dir's `conftest.py` (e.g. [`src/pyleans/test/conftest.py`](../src/pyleans/test/conftest.py) owns `FakeStorageProvider` and the regression guard).
- Do not create multiple `conftest.py` files at sibling dir depths with overlapping fixture names — pytest loads them all under the `conftest` module name and the last-loaded one wins in `sys.modules`, breaking `from conftest import X` in other testpaths. If a fixture is needed by more than one testpath, put it at the repo root instead.

Fixtures that hold resources (files, simulators, etc.) use `yield`:

```python
@pytest.fixture
def network() -> Iterator[InMemoryNetwork]:
    yield InMemoryNetwork()
```

Teardown code goes after the `yield`.

---

## 5. Test naming

- **Module**: `test_<module_under_test>.py`, mirroring the package layout.
- **Class** (optional): group tests for one feature with `TestCamelCaseName`.
- **Function**: `test_<what>_<condition>_<expected>`.

Examples:

```python
test_increment_unbounded_counter_returns_new_value
test_open_connection_to_unbound_address_raises_connection_refused
test_simulate_reset_unblocks_pending_drain
```

Avoid generic names like `test_basic` or `test_1` — the name is the first documentation a future reader sees.

---

## 6. Teardown and determinism

- Never use `time.sleep()` in production code paths; in tests, prefer `asyncio.Event` (or explicit awaitables) over `await asyncio.sleep(...)` for "wait until something happens". A sleep that's too short is flaky; a sleep that's too long is wasted time on every CI run.
- If a test's handler blocks indefinitely (e.g. simulates a stalled connection), the test owns a release mechanism — typically `release = asyncio.Event()` that the handler awaits and the test sets in `finally`. See [`test_memory_network.py::TestSimulateReset::test_simulate_reset_unblocks_pending_drain`](../src/pyleans/test/test_memory_network.py) for the canonical pattern.
- `InMemoryServer.wait_closed()` awaits in-flight handlers without cancelling them (asyncio-parity). Any test using it must release its handlers before calling it, or the test will block for as long as the handler decides to run.

---

## 7. Anti-patterns (don't)

| Anti-pattern | Why it's wrong | Do instead |
|---|---|---|
| Missing `# Arrange` / `# Act` / `# Assert` labels | Forces readers to re-derive the phase boundaries every time. | Add the labels, always. |
| Multiple Act steps per test | Obscures which action is actually being tested. | Split into separate tests. |
| `asyncio.sleep(N)` as a synchronization primitive | Flaky on slow machines, slow on fast ones. | Use `asyncio.Event`, `asyncio.wait_for`, or poll with a bounded timeout. |
| Mocking a collaborator you own | Couples the test to implementation, not behavior. | Use a fake that implements the port. |
| Asserting on internal state rather than observable behavior | Breaks on refactors that preserve behavior. | Assert on public API output. |
| One giant test covering five scenarios | Failure message tells you which line, not which scenario. | One behavior per test. |
| Sharing mutable state between tests via module globals | Order-dependent, parallelization-breaking. | Use fixtures with `yield` teardown, or reset in `autouse` fixtures. |
| `test_stuff` / `test_1` / `test_it_works` | Name tells future readers nothing. | `test_<what>_<condition>_<expected>`. |
| Binding a real port in a non-adapter unit test | Breaks the zero-OS-ports contract; risks flakiness from port-in-use on loaded hosts. | Use the `network` fixture. |
| Committing a test that triggers the regression guard | Means someone bypassed the port. | Route the test through `INetwork` instead. |

---

## 8. Pre-commit checklist for each new or modified test

- [ ] Phase labels (`# Arrange` / `# Act` / `# Assert`) are present (Arrange may be elided only when empty).
- [ ] Exactly one Act step.
- [ ] Name follows `test_<what>_<condition>_<expected>`.
- [ ] Uses the `network` fixture if it touches anything network-adjacent.
- [ ] Uses fakes from `conftest.py` or creates one locally — not mocks.
- [ ] No `time.sleep` or `asyncio.sleep` used for synchronization (only for simulating passage of time in a deterministic test).
- [ ] Passes alongside the rest of the suite in under 1 s (unit) / under a few seconds (integration).
- [ ] `ruff`, `pylint` (10.00/10), `mypy` clean.
