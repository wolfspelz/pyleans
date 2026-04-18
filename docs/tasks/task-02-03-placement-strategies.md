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

- [ ] `PlacementStrategy` is an ABC; instantiating it raises `TypeError`
- [ ] `RandomPlacement.pick_silo(g, caller, [s1,s2,s3])` returns one of the three silos
- [ ] `RandomPlacement` returns the same silo on first call for identical inputs (deterministic first-attempt)
- [ ] `PreferLocalPlacement` returns `caller` when `caller in active_silos`
- [ ] `PreferLocalPlacement` falls back to random when `caller is None` or `caller not in active_silos`
- [ ] Empty `active_silos` raises `NoSilosAvailableError`
- [ ] `@placement(RandomPlacement)` attaches `_placement_strategy` to the grain class
- [ ] Grain without `@placement` defaults to `RandomPlacement`
- [ ] Unit tests cover happy path, empty list, caller-not-in-list, determinism, decorator behavior

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation
_To be filled when task is complete._
