"""Runtime narrowing for values decoded from JSON."""

from __future__ import annotations

import json

from typing_extensions import TypeIs


def is_json_object(value: object) -> TypeIs[dict[str, object]]:
    """Return whether *value* is a JSON object."""
    return isinstance(value, dict)


def json_object(value: object) -> dict[str, object] | None:
    """Narrow a JSON value to an object, or return ``None``."""
    return value if is_json_object(value) else None


def _is_json_list(value: object) -> TypeIs[list[object]]:
    return isinstance(value, list)


def is_object_sequence(value: object) -> TypeIs[list[object] | tuple[object, ...]]:
    """Return whether *value* is a list or tuple with object-level typing."""
    return isinstance(value, list | tuple)


def tuple_or_empty(value: object) -> object:
    """Return a list/tuple as a tuple, or an empty tuple for another shape."""
    return tuple(value) if is_object_sequence(value) else ()


def json_list(value: object) -> list[object] | None:
    """Narrow a JSON value to an array, or return ``None``."""
    return value if _is_json_list(value) else None


def string_or_empty(value: object) -> object:
    """Return a JSON string unchanged, or an empty string for another shape."""
    return value if isinstance(value, str) else ""


def int_or_none(value: object) -> object:
    """Return a JSON integer, excluding booleans, or ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def float_or_none(value: object) -> object:
    """Return a JSON number as a float, excluding booleans, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def loads_json_object(text: str) -> dict[str, object] | None:
    """Decode one JSON object, returning ``None`` for invalid or non-object input."""
    try:
        value: object = json.loads(text)
    except ValueError:
        return None
    return json_object(value)
