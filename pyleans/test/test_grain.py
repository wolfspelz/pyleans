"""Tests for pyleans.grain — grain decorator and registry."""

from dataclasses import dataclass

import pytest

from pyleans.errors import GrainNotFoundError
from pyleans.grain import (
    LIFECYCLE_METHODS,
    _grain_registry,
    get_grain_class,
    get_grain_methods,
    grain,
)


@dataclass
class CounterState:
    value: int = 0


# Clear registry between tests to avoid pollution
@pytest.fixture(autouse=True)
def _clear_registry() -> None:  # type: ignore[misc]
    _grain_registry.clear()


class TestGrainDecorator:
    def test_decorator_without_args(self) -> None:
        @grain
        class MyGrain:
            async def do_something(self) -> None:
                pass

        assert MyGrain._grain_type == "MyGrain"  # type: ignore[attr-defined]
        assert MyGrain._state_type is None  # type: ignore[attr-defined]
        assert MyGrain._storage_name == "default"  # type: ignore[attr-defined]

    def test_decorator_with_state_type(self) -> None:
        @grain(state_type=CounterState)
        class CounterGrain:
            async def get_value(self) -> int:
                return 0

        assert CounterGrain._grain_type == "CounterGrain"  # type: ignore[attr-defined]
        assert CounterGrain._state_type is CounterState  # type: ignore[attr-defined]
        assert CounterGrain._storage_name == "default"  # type: ignore[attr-defined]

    def test_decorator_with_storage_name(self) -> None:
        @grain(state_type=CounterState, storage="redis")
        class CounterGrain:
            async def get_value(self) -> int:
                return 0

        assert CounterGrain._storage_name == "redis"  # type: ignore[attr-defined]

    def test_decorator_returns_same_class(self) -> None:
        @grain
        class MyGrain:
            async def do_something(self) -> None:
                pass

        assert MyGrain.__name__ == "MyGrain"
        instance = MyGrain()
        assert isinstance(instance, MyGrain)

    def test_class_is_still_instantiable(self) -> None:
        @grain
        class MyGrain:
            def __init__(self) -> None:
                self.x = 42

            async def do_something(self) -> None:
                pass

        g = MyGrain()
        assert g.x == 42


class TestGrainRegistry:
    def test_grain_registered(self) -> None:
        @grain
        class MyGrain:
            async def do_something(self) -> None:
                pass

        assert "MyGrain" in _grain_registry
        assert _grain_registry["MyGrain"] is MyGrain

    def test_get_grain_class(self) -> None:
        @grain
        class MyGrain:
            async def do_something(self) -> None:
                pass

        assert get_grain_class("MyGrain") is MyGrain

    def test_get_grain_class_not_found(self) -> None:
        with pytest.raises(GrainNotFoundError, match="NonExistent"):
            get_grain_class("NonExistent")

    def test_multiple_grains_registered(self) -> None:
        @grain
        class GrainA:
            async def method_a(self) -> None:
                pass

        @grain
        class GrainB:
            async def method_b(self) -> None:
                pass

        assert get_grain_class("GrainA") is GrainA
        assert get_grain_class("GrainB") is GrainB


class TestGetGrainMethods:
    def test_discovers_public_async_methods(self) -> None:
        @grain
        class MyGrain:
            async def method_a(self) -> None:
                pass

            async def method_b(self) -> int:
                return 0

        methods = get_grain_methods(MyGrain)
        assert "method_a" in methods
        assert "method_b" in methods

    def test_excludes_private_methods(self) -> None:
        @grain
        class MyGrain:
            async def public_method(self) -> None:
                pass

            async def _private_method(self) -> None:
                pass

        methods = get_grain_methods(MyGrain)
        assert "public_method" in methods
        assert "_private_method" not in methods

    def test_excludes_dunder_methods(self) -> None:
        @grain
        class MyGrain:
            async def public_method(self) -> None:
                pass

            def __init__(self) -> None:
                pass

        methods = get_grain_methods(MyGrain)
        assert "__init__" not in methods

    def test_excludes_lifecycle_hooks(self) -> None:
        @grain
        class MyGrain:
            async def on_activate(self) -> None:
                pass

            async def on_deactivate(self) -> None:
                pass

            async def do_work(self) -> None:
                pass

        methods = get_grain_methods(MyGrain)
        assert "on_activate" not in methods
        assert "on_deactivate" not in methods
        assert "do_work" in methods

    def test_excludes_sync_methods(self) -> None:
        @grain
        class MyGrain:
            def sync_method(self) -> None:
                pass

            async def async_method(self) -> None:
                pass

        methods = get_grain_methods(MyGrain)
        assert "sync_method" not in methods
        assert "async_method" in methods

    def test_empty_grain(self) -> None:
        @grain
        class EmptyGrain:
            pass

        methods = get_grain_methods(EmptyGrain)
        assert methods == {}

    def test_grain_with_only_lifecycle(self) -> None:
        @grain
        class LifecycleGrain:
            async def on_activate(self) -> None:
                pass

            async def on_deactivate(self) -> None:
                pass

        methods = get_grain_methods(LifecycleGrain)
        assert methods == {}


class TestLifecycleMethods:
    def test_lifecycle_methods_constant(self) -> None:
        assert "on_activate" in LIFECYCLE_METHODS
        assert "on_deactivate" in LIFECYCLE_METHODS
        assert len(LIFECYCLE_METHODS) == 2


class TestGrainMetadata:
    def test_stateless_grain(self) -> None:
        @grain
        class StatelessGrain:
            async def do_work(self) -> str:
                return "done"

        assert StatelessGrain._state_type is None  # type: ignore[attr-defined]

    def test_stateful_grain(self) -> None:
        @grain(state_type=CounterState)
        class StatefulGrain:
            async def get_value(self) -> int:
                return 0

        assert StatefulGrain._state_type is CounterState  # type: ignore[attr-defined]
