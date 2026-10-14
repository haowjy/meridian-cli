"""Shared model policy and normalization for operational config schemas."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel
from typing_extensions import TypeIs

from meridian.lib.utils.json_shapes import json_list


class ConfigModel(BaseModel):
    """Base for models carrying file-key warning policy."""

    config_ignored_keys: ClassVar[frozenset[str]] = frozenset()
    config_silences_extras: ClassVar[bool] = False


def normalize_required_string(raw: str, *, source: str) -> str:
    normalized = raw.strip()
    if not normalized:
        raise ValueError(f"Invalid value for '{source}': expected non-empty string.")
    return normalized


def _is_object_tuple(value: object) -> TypeIs[tuple[object, ...]]:
    return isinstance(value, tuple)


def normalize_string_sequence(
    value: object,
    *,
    source: str,
    empty_entries: str = "entries",
) -> tuple[str, ...]:
    values = json_list(value)
    if values is None:
        if not _is_object_tuple(value):
            raise ValueError(
                f"Invalid value for '{source}': expected array[str], "
                f"got {type(value).__name__} ({value!r})."
            )
        values = value

    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError(
                f"Invalid value for '{source}': expected array[str], "
                f"got {type(item).__name__} ({item!r})."
            )
        compact = item.strip()
        if not compact:
            raise ValueError(f"Invalid value for '{source}': expected non-empty {empty_entries}.")
        normalized.append(compact)
    return tuple(normalized)
