"""Tests for pyleans.grain_base — Grain[TState] base class."""

from dataclasses import dataclass

import pytest
from pyleans.errors import GrainActivationError
from pyleans.grain import _grain_registry, get_grain_methods, grain
from pyleans.grain_base import Grain
from pyleans.identity import GrainId


@dataclass
class PlayerState:
    name: str = ""
    level: int = 1


@dataclass
class CounterState:
    value: int = 0


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    _grain_registry.clear()


class TestGrainBaseStubs:
    """Stub methods raise GrainActivationError before runtime activation."""

    def test_read_state_raises_before_activation(self) -> None:
        # Arrange
        instance: Grain[CounterState] = Grain()

        # Act / Assert
        with pytest.raises(GrainActivationError, match="read_state not bound"):
            import asyncio

            asyncio.get_event_loop().run_until_complete(instance.read_state())

    def test_write_state_raises_before_activation(self) -> None:
        instance: Grain[CounterState] = Grain()
        with pytest.raises(GrainActivationError, match="write_state not bound"):
            # write_state is async, but we just need to see it raises synchronously
            # when the coroutine is awaited
            import asyncio

            asyncio.get_event_loop().run_until_complete(instance.write_state())

    def test_clear_state_raises_before_activation(self) -> None:
        instance: Grain[CounterState] = Grain()
        with pytest.raises(GrainActivationError, match="clear_state not bound"):
            import asyncio

            asyncio.get_event_loop().run_until_complete(instance.clear_state())

    def test_deactivate_on_idle_raises_before_activation(self) -> None:
        instance: Grain[CounterState] = Grain()
        with pytest.raises(GrainActivationError, match="deactivate_on_idle not bound"):
            instance.deactivate_on_idle()

    def test_identity_not_set_before_activation(self) -> None:
        instance: Grain[CounterState] = Grain()
        with pytest.raises(GrainActivationError, match="identity not available"):
            _ = instance.identity

    def test_state_not_set_before_activation(self) -> None:
        instance: Grain[CounterState] = Grain()
        with pytest.raises(AttributeError):
            _ = instance.state


class TestGrainLogger:
    """Per-grain-type logger available via self.logger."""

    def test_logger_returns_grain_type_logger(self) -> None:
        @grain
        class MyLogGrain(Grain[CounterState]):
            async def do_work(self) -> None:
                pass

        instance = MyLogGrain()
        assert instance.logger.name == "pyleans.grain.MyLogGrain"

    def test_logger_available_before_activation(self) -> None:
        instance: Grain[CounterState] = Grain()
        assert instance.logger.name == "pyleans.grain.Grain"

    def test_different_grain_types_get_different_loggers(self) -> None:
        @grain
        class GrainA(Grain[CounterState]):
            async def do_work(self) -> None:
                pass

        @grain
        class GrainB(Grain[CounterState]):
            async def do_work(self) -> None:
                pass

        assert GrainA().logger.name == "pyleans.grain.GrainA"
        assert GrainB().logger.name == "pyleans.grain.GrainB"


class TestGrainIdentityInInit:
    """Identity is available during __init__ via context var."""

    def test_identity_available_during_init(self) -> None:
        from pyleans.grain_base import _current_grain_id

        captured_key: str | None = None

        @grain
        class InitGrain(Grain[CounterState]):
            def __init__(self) -> None:
                nonlocal captured_key
                captured_key = self.identity.key

            async def do_work(self) -> None:
                pass

        gid = GrainId("InitGrain", "my-key")
        token = _current_grain_id.set(gid)
        try:
            instance = InitGrain()
        finally:
            _current_grain_id.reset(token)

        assert captured_key == "my-key"
        # After construction, the property still works (from context var during init)
        # but permanent assignment happens via setter
        instance.identity = gid
        assert instance.identity == gid

    def test_identity_falls_back_to_instance_attr(self) -> None:
        instance: Grain[CounterState] = Grain()
        gid = GrainId("TestGrain", "key-1")
        instance.identity = gid
        assert instance.identity == gid


class TestGrainBaseRuntimeBinding:
    """Runtime can override stubs with setattr."""

    def test_state_can_be_set(self) -> None:
        instance: Grain[CounterState] = Grain()
        instance.state = CounterState(value=42)
        assert instance.state.value == 42

    def test_write_state_can_be_overridden(self) -> None:
        instance: Grain[CounterState] = Grain()
        called = False

        async def mock_save() -> None:
            nonlocal called
            called = True

        instance.write_state = mock_save  # type: ignore[method-assign]
        import asyncio

        asyncio.get_event_loop().run_until_complete(instance.write_state())
        assert called

    def test_read_state_can_be_overridden(self) -> None:
        # Arrange
        instance: Grain[CounterState] = Grain()
        called = False

        async def mock_read() -> None:
            nonlocal called
            called = True

        # Act
        instance.read_state = mock_read  # type: ignore[method-assign]
        import asyncio

        asyncio.get_event_loop().run_until_complete(instance.read_state())

        # Assert
        assert called

    def test_deactivate_on_idle_can_be_overridden(self) -> None:
        instance: Grain[CounterState] = Grain()
        called = False

        def mock_deactivate() -> None:
            nonlocal called
            called = True

        instance.deactivate_on_idle = mock_deactivate  # type: ignore[method-assign]
        instance.deactivate_on_idle()
        assert called


class TestStateTypeInference:
    """The @grain decorator infers state_type from Grain[TState]."""

    def test_infer_state_type_from_generic(self) -> None:
        @grain
        class MyGrain(Grain[CounterState]):
            async def get_value(self) -> int:
                return 0

        assert MyGrain._state_type is CounterState  # type: ignore[attr-defined]

    def test_infer_state_type_with_storage(self) -> None:
        @grain(storage="redis")
        class MyGrain(Grain[PlayerState]):
            async def get_value(self) -> str:
                return ""

        assert MyGrain._state_type is PlayerState  # type: ignore[attr-defined]
        assert MyGrain._storage_name == "redis"  # type: ignore[attr-defined]

    def test_explicit_state_type_takes_precedence(self) -> None:
        @grain(state_type=PlayerState)
        class MyGrain(Grain[CounterState]):
            async def get_value(self) -> int:
                return 0

        assert MyGrain._state_type is PlayerState  # type: ignore[attr-defined]

    def test_no_base_class_no_state_type(self) -> None:
        @grain
        class StatelessGrain:
            async def do_work(self) -> str:
                return "done"

        assert StatelessGrain._state_type is None  # type: ignore[attr-defined]

    def test_grain_none_is_stateless(self) -> None:
        @grain
        class StatelessGrain(Grain[None]):
            async def do_work(self) -> str:
                return "done"

        assert StatelessGrain._state_type is None  # type: ignore[attr-defined]


class TestBaseClassMethodExclusion:
    """Base class infrastructure methods are excluded from grain interface."""

    def test_write_state_excluded_from_methods(self) -> None:
        @grain
        class MyGrain(Grain[CounterState]):
            async def get_value(self) -> int:
                return 0

        methods = get_grain_methods(MyGrain)
        assert "write_state" not in methods
        assert "clear_state" not in methods
        assert "read_state" not in methods

    def test_deactivate_on_idle_excluded(self) -> None:
        """deactivate_on_idle is sync so already excluded, but verify."""

        @grain
        class MyGrain(Grain[CounterState]):
            async def get_value(self) -> int:
                return 0

        methods = get_grain_methods(MyGrain)
        assert "deactivate_on_idle" not in methods

    def test_business_methods_still_discovered(self) -> None:
        @grain
        class MyGrain(Grain[CounterState]):
            async def get_value(self) -> int:
                return 0

            async def set_value(self, v: int) -> None:
                pass

        methods = get_grain_methods(MyGrain)
        assert "get_value" in methods
        assert "set_value" in methods


class TestGrainRegistration:
    """Grains using Grain[TState] base class register correctly."""

    def test_registered_in_registry(self) -> None:
        @grain
        class MyGrain(Grain[CounterState]):
            async def do_work(self) -> None:
                pass

        assert "MyGrain" in _grain_registry
        assert _grain_registry["MyGrain"] is MyGrain

    def test_grain_type_metadata(self) -> None:
        @grain(storage="fast")
        class MyGrain(Grain[PlayerState]):
            async def do_work(self) -> None:
                pass

        assert MyGrain._grain_type == "MyGrain"  # type: ignore[attr-defined]
        assert MyGrain._state_type is PlayerState  # type: ignore[attr-defined]
        assert MyGrain._storage_name == "fast"  # type: ignore[attr-defined]
