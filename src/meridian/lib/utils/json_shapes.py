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


def json_list(value: object) -> list[object] | None:
    """Narrow a JSON value to an array, or return ``None``."""
    return value if _is_json_list(value) else None


def loads_json_object(text: str) -> dict[str, object] | None:
    """Decode one JSON object, returning ``None`` for invalid or non-object input."""
    try:
        value: object = json.loads(text)
    except ValueError:
        return None
    return json_object(value)
