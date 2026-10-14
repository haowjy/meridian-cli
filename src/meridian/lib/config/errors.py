"""Shared diagnostics for model-backed configuration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import UnionType
from typing import TYPE_CHECKING, Union, get_args, get_origin

from pydantic import BaseModel

from meridian.lib.config.model_base import ConfigModel
from meridian.lib.utils.json_shapes import is_json_object, json_list

if TYPE_CHECKING:
    from pydantic import ValidationError

logger = logging.getLogger(__name__)

ValidationPath = tuple[str | int, ...]
ValidationPathFormatter = Callable[[ValidationPath], str]


def _nested_model_type(annotation: object) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        nested = {
            model
            for member in get_args(annotation)
            if member is not type(None) and (model := _nested_model_type(member)) is not None
        }
        return next(iter(nested)) if len(nested) == 1 else None
    if origin is tuple:
        members = get_args(annotation)
        if len(members) == 2 and members[1] is Ellipsis:
            return _nested_model_type(members[0])
    return None


def validation_error_path(location: ValidationPath) -> str:
    """Render a Pydantic error location as a user-facing config key."""

    parts: list[str] = []
    for item in location:
        if isinstance(item, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{item + 1}]"
            else:
                parts.append(f"[{item + 1}]")
            continue
        parts.append(item)
    return ".".join(parts).replace(".[", "[") or "config"


def config_validation_error(
    error: ValidationError,
    *,
    source: str | None = None,
    path_formatter: ValidationPathFormatter = validation_error_path,
) -> ValueError:
    """Translate one Pydantic failure into a concise config-key error."""

    detail = error.errors(include_url=False)[0]
    path = path_formatter(detail["loc"])
    if source is not None:
        if path == "config":
            path = source
        elif path.startswith("["):
            path = f"{source}{path}"
        else:
            path = f"{source}.{path}"
    error_type = detail["type"]
    raw_value = detail.get("input")
    got = f"{type(raw_value).__name__} ({raw_value!r})"

    expected_by_type = {
        "bool_type": "bool",
        "dict_type": "table",
        "float_type": "float",
        "int_type": "int",
        "list_type": "array",
        "model_type": "table",
        "string_type": "str",
        "tuple_type": "array",
    }
    expected = expected_by_type.get(error_type)
    if expected is not None:
        return ValueError(f"Invalid value for '{path}': expected {expected}, got {got}.")

    if error_type == "string_too_short":
        return ValueError(f"Invalid value for '{path}': expected non-empty string.")
    if error_type == "extra_forbidden":
        return ValueError(f"Invalid value for '{path}': unknown key.")
    if error_type == "missing":
        return ValueError(f"Invalid value for '{path}': required value is missing.")

    context = detail.get("ctx")
    cause = context.get("error") if context is not None else None
    if cause is not None:
        problem = str(cause).removesuffix(".")
        if problem.startswith("Invalid value for "):
            return ValueError(f"{problem}.")
        return ValueError(f"Invalid value for '{path}': {problem}.")

    message = detail["msg"].removeprefix("Input should be ").removesuffix(".")
    return ValueError(f"Invalid value for '{path}': expected {message}, got {got}.")


def warn_unknown_model_keys(
    raw_value: object,
    *,
    model: type[BaseModel],
    source: str,
) -> None:
    """Warn for ignored Pydantic extras without duplicating field validation."""

    root_field = model.model_fields.get("root")
    root_model = (
        _nested_model_type(root_field.annotation) if root_field is not None else None
    )
    root_items = json_list(raw_value)
    if root_model is not None and root_items is not None:
        for index, item in enumerate(root_items, start=1):
            warn_unknown_model_keys(item, model=root_model, source=f"{source}[{index}]")
        return

    if not is_json_object(raw_value):
        return

    extra_behavior = model.model_config.get("extra")
    ignored_keys: frozenset[str] = (
        model.config_ignored_keys if issubclass(model, ConfigModel) else frozenset()
    )
    silences_extras = model.config_silences_extras if issubclass(model, ConfigModel) else False
    for key, value in raw_value.items():
        field = model.model_fields.get(key)
        if field is not None:
            nested_model = _nested_model_type(field.annotation)
            if nested_model is not None:
                warn_unknown_model_keys(
                    value,
                    model=nested_model,
                    source=f"{source}.{key}",
                )
            continue
        if key not in ignored_keys and not silences_extras and extra_behavior != "forbid":
            logger.warning("Ignoring unknown Meridian config key '%s.%s'.", source, key)
