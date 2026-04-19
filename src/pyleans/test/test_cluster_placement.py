"""Tests for pyleans.cluster.placement — placement strategies + decorator."""

import pytest
from pyleans.cluster.placement import (
    DEFAULT_PLACEMENT_ATTR,
    NoSilosAvailableError,
    PlacementStrategy,
    PreferLocalPlacement,
    RandomPlacement,
    get_placement_strategy,
    placement,
)
from pyleans.identity import GrainId, SiloAddress


def _silo(host: str, port: int = 11111, epoch: int = 1_700_000_000) -> SiloAddress:
    return SiloAddress(host=host, port=port, epoch=epoch)


def _grain(grain_type: str = "CounterGrain", key: str = "a") -> GrainId:
    return GrainId(grain_type, key)


class TestPlacementStrategyAbc:
    def test_instantiating_abc_raises_type_error(self) -> None:
        # Act / Assert
        with pytest.raises(TypeError):
            PlacementStrategy()  # type: ignore[abstract]


class TestRandomPlacementHappyPath:
    def test_pick_returns_member_of_active_silos(self) -> None:
        # Arrange
        silos = [_silo("10.0.0.1"), _silo("10.0.0.2"), _silo("10.0.0.3")]
        strategy = RandomPlacement()

        # Act
        pick = strategy.pick_silo(_grain(), None, silos)

        # Assert
        assert pick in silos

    def test_single_silo_always_picked(self) -> None:
        # Arrange
        silo = _silo("10.0.0.1")
        strategy = RandomPlacement()

        # Act
        pick = strategy.pick_silo(_grain(), None, [silo])

        # Assert
        assert pick == silo


class TestRandomPlacementDeterminism:
    def test_identical_inputs_return_identical_pick(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 6)]
        strategy_a = RandomPlacement()
        strategy_b = RandomPlacement()
        grain_id = _grain("CounterGrain", "alpha")

        # Act
        pick_a = strategy_a.pick_silo(grain_id, None, silos)
        pick_b = strategy_b.pick_silo(grain_id, None, silos)

        # Assert
        assert pick_a == pick_b

    def test_silo_list_order_does_not_affect_pick(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 6)]
        strategy = RandomPlacement()
        grain_id = _grain("CounterGrain", "beta")

        # Act
        pick_ordered = strategy.pick_silo(grain_id, None, silos)
        pick_reversed = strategy.pick_silo(grain_id, None, list(reversed(silos)))

        # Assert
        assert pick_ordered == pick_reversed

    def test_different_grain_ids_may_pick_different_silos(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 6)]
        strategy = RandomPlacement()

        # Act
        picks = {
            strategy.pick_silo(GrainId("CounterGrain", f"k-{i}"), None, silos) for i in range(50)
        }

        # Assert
        assert len(picks) > 1

    def test_retry_can_diverge_from_first_attempt(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 6)]
        strategy = RandomPlacement()
        grain_id = _grain("CounterGrain", "retry-key")
        first = strategy.pick_silo(grain_id, None, silos)

        # Act
        retries = {strategy.pick_silo_retry(grain_id, silos) for _ in range(40)}

        # Assert
        assert retries - {first}


class TestRandomPlacementEmpty:
    def test_empty_active_silos_raises_no_silos_available(self) -> None:
        # Arrange
        strategy = RandomPlacement()

        # Act / Assert
        with pytest.raises(NoSilosAvailableError):
            strategy.pick_silo(_grain(), None, [])

    def test_empty_retry_raises_no_silos_available(self) -> None:
        # Arrange
        strategy = RandomPlacement()

        # Act / Assert
        with pytest.raises(NoSilosAvailableError):
            strategy.pick_silo_retry(_grain(), [])


class TestPreferLocalPlacement:
    def test_caller_in_active_returns_caller(self) -> None:
        # Arrange
        caller = _silo("10.0.0.2")
        silos = [_silo("10.0.0.1"), caller, _silo("10.0.0.3")]
        strategy = PreferLocalPlacement()

        # Act
        pick = strategy.pick_silo(_grain(), caller, silos)

        # Assert
        assert pick == caller

    def test_caller_none_falls_back_to_random(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 4)]
        strategy = PreferLocalPlacement()
        grain_id = _grain("CounterGrain", "no-caller")
        expected = RandomPlacement().pick_silo(grain_id, None, silos)

        # Act
        pick = strategy.pick_silo(grain_id, None, silos)

        # Assert
        assert pick == expected

    def test_caller_not_in_active_falls_back_to_random(self) -> None:
        # Arrange
        silos = [_silo(f"10.0.0.{i}") for i in range(1, 4)]
        stale_caller = _silo("10.0.0.99")
        strategy = PreferLocalPlacement()
        grain_id = _grain("CounterGrain", "stale-caller")
        expected = RandomPlacement().pick_silo(grain_id, stale_caller, silos)

        # Act
        pick = strategy.pick_silo(grain_id, stale_caller, silos)

        # Assert
        assert pick == expected

    def test_empty_active_silos_raises_no_silos_available(self) -> None:
        # Arrange
        strategy = PreferLocalPlacement()

        # Act / Assert
        with pytest.raises(NoSilosAvailableError):
            strategy.pick_silo(_grain(), _silo("10.0.0.1"), [])


class TestPlacementDecorator:
    def test_decorator_attaches_strategy_class(self) -> None:
        # Arrange
        @placement(RandomPlacement)
        class DecoratedGrain:
            pass

        # Act
        attached = getattr(DecoratedGrain, DEFAULT_PLACEMENT_ATTR, None)

        # Assert
        assert attached is RandomPlacement

    def test_decorator_returns_same_class(self) -> None:
        # Arrange
        class Target:
            pass

        # Act
        decorated = placement(RandomPlacement)(Target)

        # Assert
        assert decorated is Target

    def test_decorator_rejects_non_strategy_subclass(self) -> None:
        # Arrange
        class NotAStrategy:
            pass

        # Act / Assert
        with pytest.raises(TypeError):
            placement(NotAStrategy)  # type: ignore[type-var]

    def test_decorator_rejects_instance(self) -> None:
        # Arrange
        instance = RandomPlacement()

        # Act / Assert
        with pytest.raises(TypeError):
            placement(instance)  # type: ignore[arg-type]

    def test_get_placement_strategy_uses_decorated_class(self) -> None:
        # Arrange
        @placement(PreferLocalPlacement)
        class LocalGrain:
            pass

        # Act
        strategy = get_placement_strategy(LocalGrain)

        # Assert
        assert isinstance(strategy, PreferLocalPlacement)

    def test_get_placement_strategy_default_is_random(self) -> None:
        # Arrange
        class PlainGrain:
            pass

        # Act
        strategy = get_placement_strategy(PlainGrain)

        # Assert
        assert isinstance(strategy, RandomPlacement)

    def test_get_placement_strategy_returns_fresh_instance(self) -> None:
        # Arrange
        @placement(RandomPlacement)
        class DecoratedGrain:
            pass

        # Act
        first = get_placement_strategy(DecoratedGrain)
        second = get_placement_strategy(DecoratedGrain)

        # Assert
        assert first is not second
