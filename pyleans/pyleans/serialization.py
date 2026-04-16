"""Pluggable serialization layer with JSON/orjson default."""

import dataclasses
from abc import ABC, abstractmethod
from typing import Any

import orjson

from pyleans.errors import SerializationError


class Serializer(ABC):
    """Pluggable serialization interface."""

    @abstractmethod
    def serialize(self, obj: Any) -> bytes:
        """Serialize an object to bytes."""
        ...

    @abstractmethod
    def deserialize[T](self, data: bytes, target_type: type[T]) -> T:
        """Deserialize bytes into an instance of target_type."""
        ...


def _dataclass_to_dict(obj: Any) -> Any:
    """Recursively convert a dataclass to a dict suitable for JSON serialization."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            field.name: _dataclass_to_dict(getattr(obj, field.name))
            for field in dataclasses.fields(obj)
        }
    if isinstance(obj, list):
        return [_dataclass_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _dataclass_to_dict(value) for key, value in obj.items()}
    return obj


def _dict_to_dataclass[T](data: Any, target_type: type[T]) -> T:
    """Recursively reconstruct a dataclass from a dict."""
    if not dataclasses.is_dataclass(target_type):
        return data  # type: ignore[no-any-return]

    field_values: dict[str, Any] = {}
    for field in dataclasses.fields(target_type):
        if field.name not in data:
            continue
        value = data[field.name]
        field_type = field.type

        # Resolve string annotations to actual types if needed
        if isinstance(field_type, str):
            field_values[field.name] = value
            continue

        if dataclasses.is_dataclass(field_type) and isinstance(value, dict):
            field_values[field.name] = _dict_to_dataclass(value, field_type)
        else:
            field_values[field.name] = value

    return target_type(**field_values)


class JsonSerializer(Serializer):
    """Default JSON serializer using orjson for speed."""

    def serialize(self, obj: Any) -> bytes:
        """Serialize an object to JSON bytes.

        Handles dataclasses, primitives, lists, dicts, and None.
        """
        try:
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return orjson.dumps(_dataclass_to_dict(obj))
            return orjson.dumps(obj)
        except TypeError as e:
            raise SerializationError(f"Cannot serialize {type(obj).__name__}: {e}") from e

    def deserialize[T](self, data: bytes, target_type: type[T]) -> T:
        """Deserialize JSON bytes into an instance of target_type."""
        try:
            parsed = orjson.loads(data)
        except orjson.JSONDecodeError as e:
            raise SerializationError(f"Invalid JSON: {e}") from e

        if dataclasses.is_dataclass(target_type):
            if not isinstance(parsed, dict):
                raise SerializationError(
                    f"Expected dict for {target_type.__name__}, got {type(parsed).__name__}"
                )
            try:
                return _dict_to_dataclass(parsed, target_type)
            except (TypeError, KeyError) as e:
                raise SerializationError(
                    f"Cannot deserialize into {target_type.__name__}: {e}"
                ) from e

        return parsed  # type: ignore[no-any-return]
