# Task 02-03: Grain Placement Strategies -- Random and PreferLocal

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-02-01-cluster-identity.md](task-02-01-cluster-identity.md)

## References
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- §7 Grain Placement
- [plan.md](../plan.md) -- Phase 2 item 10 (placement: random + prefer-local)

## Description

When a grain has no activation anywhere in the cluster, the grain directory (task-02-13) must pick a silo to host it. Orleans exposes this as a `PlacementStrategy` abstraction and ships several implementations; pyleans PoC ships two: **random** (baseline) and **prefer-local** (keeps chattiness local when the caller is already on a valid silo).

Placement is a pure decision layer -- it takes (grain_id, caller_silo, active_silos) and returns a silo. It does not know about the directory, the transport, or networking. This keeps it unit-testable in isolation and replaceable per grain type via a `@placement` decorator, matching Orleans' `[RandomPlacement]` / `[PreferLocalPlacement]` attributes.

### Files to create

- `src/pyleans/pyleans/cluster/placement.py`

### Design

```python
# src/pyleans/pyleans/cluster/placement.py
from abc import ABC, abstractmethod
from pyleans.identity import GrainId, SiloAddress


class PlacementStrategy(ABC):
    """Decides which silo a new grain activation lands on."""

    @abstractmethod
    def pick_silo(
        self,
        grain_id: GrainId,
        caller: SiloAddress | None,
        active_silos: list[SiloAddress],
    ) -> SiloAddress:
        """Return the silo that should host a new activation of `grain_id`.

        Raises NoSilosAvailableError if active_silos is empty.
        `caller` is the silo making the request (None for external clients).
        """


class RandomPlacement(PlacementStrategy):
    """Uniform random selection from active silos."""


class PreferLocalPlacement(PlacementStrategy):
    """Use `caller` if it is in active_silos; otherwise fall back to random.

    Caller is a grain-call origin -- when a grain on silo A activates another
    grain, PreferLocal keeps the new activation on A so they can talk locally
    without a network hop. When the call originates from an external client
    (caller is None), it falls back to random.
    """
```

Registration and lookup use a decorator:

```python
from pyleans.cluster.placement import placement, RandomPlacement, PreferLocalPlacement

@placement(PreferLocalPlacement)
@grain(state_type=CounterState, storage="default")
class CounterGrain(Grain[CounterState]):
    ...
```

The `placement` decorator attaches the strategy class to the grain class attribute `_placement_strategy` (instantiated lazily). The directory (task-02-13) calls `grain_class._placement_strategy.pick_silo(...)` when resolving a missing activation. Grains without an explicit decorator default to `RandomPlacement`.

### Determinism and randomness

`RandomPlacement` uses `random.Random` seeded from `hash_grain_id(grain_id)` for a first attempt, then a local non-seeded `random.Random` for retries. Rationale: deterministic-on-first-attempt means two clients that race to activate the same missing grain land on the same silo and cleanly de-duplicate at the directory owner (a subtle but important win). Retry after failure picks independently so we don't hammer the same silo.

### Error cases

```python
class NoSilosAvailableError(RuntimeError):
    """Raised when placement is asked to pick from an empty silo list."""
```

The directory (task-02-13) catches this and surfaces it to the caller as a `ClusterNotReadyError` -- distinguishing "no silos" (configuration / startup) from "all silos unreachable" (network) helps operations.

### Acceptance criteria

- [x] `PlacementStrategy` is an ABC; instantiating it raises `TypeError`
- [x] `RandomPlacement.pick_silo(g, caller, [s1,s2,s3])` returns one of the three silos
- [x] `RandomPlacement` returns the same silo on first call for identical inputs (deterministic first-attempt)
- [x] `PreferLocalPlacement` returns `caller` when `caller in active_silos`
- [x] `PreferLocalPlacement` falls back to random when `caller is None` or `caller not in active_silos`
- [x] Empty `active_silos` raises `NoSilosAvailableError`
- [x] `@placement(RandomPlacement)` attaches `_placement_strategy` to the grain class
- [x] Grain without `@placement` defaults to `RandomPlacement`
- [x] Unit tests cover happy path, empty list, caller-not-in-list, determinism, decorator behavior

## Findings of code review

- **Clean code / SOLID / DRY / YAGNI / KISS**: `PlacementStrategy` is a single-method ABC; `RandomPlacement` and `PreferLocalPlacement` each have one responsibility; the decorator is a five-line function; `_require_silos` centralises the empty-list check.
- **Type hints**: fully typed; `placement()` is generic over `TGrain` to preserve the decorated class's type; mypy strict clean.
- **Hexagonal architecture**: module lives in `pyleans.cluster` alongside the ring — no networking, no I/O, no directory coupling. Downstream directory (task-02-13) injects the strategy.
- **Naming**: `PlacementStrategy`, `RandomPlacement`, `PreferLocalPlacement` PascalCase; `pick_silo`, `pick_silo_retry`, `get_placement_strategy` snake_case. `DEFAULT_PLACEMENT_ATTR` constant isolates the grain-class attribute name so test and runtime code share the same identifier.
- **Tests**: AAA labels, one Act per test, descriptive names; covers ABC instantiation, happy paths, determinism (including order-invariance of the silo list), retry divergence from the first-attempt seed, empty-list error, all three fallback branches of `PreferLocalPlacement`, decorator happy path / rejection of non-strategy types / rejection of instances, `get_placement_strategy` default and decorated paths, fresh-instance semantics.
- **Logging**: DEBUG level for per-pick events (picks fire per grain activation, well under 1/sec/grain but potentially high aggregate volume — DEBUG is correct).
- **No dead code or unused imports.**

No issues raised.

## Findings of security review

- **Input validation at the boundary**: empty `active_silos` is rejected; `placement()` validates that its argument is a `PlacementStrategy` subclass (not an instance, not an unrelated class).
- **Unbounded resource consumption**: strategy instances hold a single `random.Random` (retry RNG); no growing state.
- **Deterministic RNG**: first-attempt seed is derived from `stable_hash(grain_id)` — no HashDoS exposure because the seed input is internal `GrainId` state, not untrusted input.
- **No file, network, or subprocess I/O**; no secrets logged.

No vulnerabilities found.

## Summary of implementation

### Files created / modified

- [src/pyleans/pyleans/cluster/placement.py](../../src/pyleans/pyleans/cluster/placement.py) — new — `PlacementStrategy` ABC, `RandomPlacement`, `PreferLocalPlacement`, `NoSilosAvailableError`, `placement` decorator, `get_placement_strategy` lookup.
- [src/pyleans/pyleans/cluster/__init__.py](../../src/pyleans/pyleans/cluster/__init__.py) — extended — re-exports placement symbols alongside identity and ring.
- [src/pyleans/test/test_cluster_placement.py](../../src/pyleans/test/test_cluster_placement.py) — new — 20 tests.

### Key decisions

- **Deterministic first-attempt, unseeded retry.** First-attempt pick derives its RNG seed from `hash_grain_id(grain_id)` so racing clients converge on the same silo and de-duplicate at the directory owner. Retries use a non-seeded `random.Random` instance — not the global `random` module — so the production loop and the test harness can both run without global RNG side effects.
- **Sort before seeded choice.** `RandomPlacement._first_attempt_pick` sorts `active_silos` by `silo_id` before calling `seeded.choice`. Without the sort, two callers with the same grain id but different silo-list ordering would see different picks. With the sort, determinism depends only on the set of active silos.
- **Decorator attaches the strategy *class*, not an instance.** Grain classes are loaded once at import time; picking a strategy at runtime preserves flexibility (e.g. strategies that read config on construction) without coupling import-time state to runtime configuration.
- **`NoSilosAvailableError` derives from `RuntimeError`, not `PyleansError`.** The task spec prescribes this; the distinction exists because no-silos is a cluster-readiness problem surfaced separately from directory / transport errors.

### Deviations

None. Task spec acceptance criteria are all met; a `pick_silo_retry` helper was added on `RandomPlacement` so callers that need non-seeded picks do not have to instantiate a second strategy or reach into global RNG state.

### Test coverage summary

20 tests: ABC refusal to instantiate; happy path for random and prefer-local; deterministic first-attempt pick across equal, order-varied, and fresh strategy instances; distribution across 50 grain ids; retry divergence from first-attempt pick; empty-silo-list raises in both strategies; prefer-local falls back when caller is `None` and when caller is stale; decorator attaches the class / returns the same class / rejects non-strategy types / rejects instances; `get_placement_strategy` returns decorated class instance, falls back to random, returns a fresh instance each call.

Full suite: **495 tests** pass; `ruff check`, `ruff format --check`, `pylint` 10.00/10, `mypy` strict all clean.
