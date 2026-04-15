"""Tests for pyleans.serialization — pluggable serialization layer."""

from dataclasses import dataclass, field

import pytest

from pyleans.errors import SerializationError
from pyleans.serialization import JsonSerializer, Serializer


@dataclass
class SimpleState:
    name: str = ""
    level: int = 1


@dataclass
class NestedState:
    player: SimpleState = field(default_factory=SimpleState)
    score: int = 0


@dataclass
class ListState:
    items: list[str] = field(default_factory=list)
    count: int = 0


class TestSerializerABC:
    def test_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            Serializer()  # type: ignore[abstract]

    def test_custom_implementation(self) -> None:
        class DummySerializer(Serializer):
            def serialize(self, obj: object) -> bytes:
                return b"dummy"

            def deserialize(self, data: bytes, target_type: type) -> object:
                return "dummy"

        s = DummySerializer()
        assert s.serialize("anything") == b"dummy"
        assert s.deserialize(b"anything", str) == "dummy"


class TestJsonSerializerPrimitives:
    def setup_method(self) -> None:
        self.serializer = JsonSerializer()

    def test_string(self) -> None:
        data = self.serializer.serialize("hello")
        result = self.serializer.deserialize(data, str)
        assert result == "hello"

    def test_int(self) -> None:
        data = self.serializer.serialize(42)
        result = self.serializer.deserialize(data, int)
        assert result == 42

    def test_float(self) -> None:
        data = self.serializer.serialize(3.14)
        result = self.serializer.deserialize(data, float)
        assert result == pytest.approx(3.14)

    def test_bool(self) -> None:
        data = self.serializer.serialize(True)
        result = self.serializer.deserialize(data, bool)
        assert result is True

    def test_none(self) -> None:
        data = self.serializer.serialize(None)
        result = self.serializer.deserialize(data, type(None))
        assert result is None

    def test_list(self) -> None:
        data = self.serializer.serialize([1, 2, 3])
        result = self.serializer.deserialize(data, list)
        assert result == [1, 2, 3]

    def test_dict(self) -> None:
        data = self.serializer.serialize({"a": 1, "b": 2})
        result = self.serializer.deserialize(data, dict)
        assert result == {"a": 1, "b": 2}

    def test_empty_list(self) -> None:
        data = self.serializer.serialize([])
        result = self.serializer.deserialize(data, list)
        assert result == []

    def test_empty_dict(self) -> None:
        data = self.serializer.serialize({})
        result = self.serializer.deserialize(data, dict)
        assert result == {}


class TestJsonSerializerDataclass:
    def setup_method(self) -> None:
        self.serializer = JsonSerializer()

    def test_simple_dataclass_roundtrip(self) -> None:
        state = SimpleState(name="Alice", level=5)
        data = self.serializer.serialize(state)
        result = self.serializer.deserialize(data, SimpleState)
        assert result == state

    def test_dataclass_default_values(self) -> None:
        state = SimpleState()
        data = self.serializer.serialize(state)
        result = self.serializer.deserialize(data, SimpleState)
        assert result == state
        assert result.name == ""
        assert result.level == 1

    def test_nested_dataclass_roundtrip(self) -> None:
        state = NestedState(player=SimpleState(name="Bob", level=10), score=999)
        data = self.serializer.serialize(state)
        result = self.serializer.deserialize(data, NestedState)
        assert result == state
        assert result.player.name == "Bob"
        assert result.player.level == 10

    def test_dataclass_with_list(self) -> None:
        state = ListState(items=["sword", "shield"], count=2)
        data = self.serializer.serialize(state)
        result = self.serializer.deserialize(data, ListState)
        assert result == state

    def test_dataclass_with_empty_list(self) -> None:
        state = ListState()
        data = self.serializer.serialize(state)
        result = self.serializer.deserialize(data, ListState)
        assert result == state

    def test_serialized_output_is_bytes(self) -> None:
        data = self.serializer.serialize(SimpleState())
        assert isinstance(data, bytes)

    def test_serialized_output_is_valid_json(self) -> None:
        import json

        data = self.serializer.serialize(SimpleState(name="test", level=3))
        parsed = json.loads(data)
        assert parsed == {"name": "test", "level": 3}


class TestJsonSerializerErrors:
    def setup_method(self) -> None:
        self.serializer = JsonSerializer()

    def test_non_serializable_raises(self) -> None:
        with pytest.raises(SerializationError, match="Cannot serialize"):
            self.serializer.serialize(object())

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(SerializationError, match="Invalid JSON"):
            self.serializer.deserialize(b"not json", SimpleState)

    def test_wrong_structure_for_dataclass_raises(self) -> None:
        with pytest.raises(SerializationError, match="Expected dict"):
            self.serializer.deserialize(b"[1,2,3]", SimpleState)

    def test_serialization_error_is_pyleans_error(self) -> None:
        from pyleans.errors import PyleansError

        with pytest.raises(PyleansError):
            self.serializer.serialize(object())
