from __future__ import annotations

import json

from typing_extensions import TypeIs


def is_json_object(value: object) -> TypeIs[dict[str, object]]:
    return isinstance(value, dict)


def json_object(value: object) -> dict[str, object] | None:
    return value if is_json_object(value) else None


def _is_json_list(value: object) -> TypeIs[list[object]]:
    return isinstance(value, list)


def is_object_sequence(value: object) -> TypeIs[list[object] | tuple[object, ...]]:
    return isinstance(value, list | tuple)


def tuple_or_empty(value: object) -> object:
    return tuple(value) if is_object_sequence(value) else ()


def json_list(value: object) -> list[object] | None:
    return value if _is_json_list(value) else None


def string_or_empty(value: object) -> object:
    return value if isinstance(value, str) else ""


def int_or_none(value: object) -> object:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def float_or_none(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def loads_json_object(text: str) -> dict[str, object] | None:
    try:
        value: object = json.loads(text)
    except ValueError:
        return None
    return json_object(value)
