"""Shared diagnostics for model-backed configuration."""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel, RootModel, ValidationError
from typing_extensions import TypeIs

from meridian.lib.config.model_base import ConfigModel
from meridian.lib.utils.json_shapes import is_json_object, is_object_sequence

logger = logging.getLogger(__name__)

ValidationPath = tuple[str | int, ...]
ValidationPathFormatter = Callable[[ValidationPath], str]


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
    """Warn for keys Pydantic would ignore, using Pydantic's own traversal.

    Validates once with extras kept, then reports ``model_extra`` from the
    validated tree. Invalid input warns nothing: the authoritative validation
    that follows reports the real error.
    """

    try:
        validated = model.model_validate(raw_value, extra="allow")
    except ValidationError:
        return
    _warn_extras(validated, source)


def _is_object_root_model(value: object) -> TypeIs[RootModel[object]]:
    return isinstance(value, RootModel)


def _warn_extras(value: object, path: str) -> None:
    if _is_object_root_model(value):
        _warn_extras(value.root, path)
    elif isinstance(value, BaseModel):
        model = type(value)
        silenced = model.model_config.get("extra") == "forbid" or (
            issubclass(model, ConfigModel) and model.config_silences_extras
        )
        for key in () if silenced else (value.model_extra or {}):
            logger.warning("Ignoring unknown Meridian config key '%s.%s'.", path, key)
        for name in model.model_fields:
            _warn_extras(getattr(value, name), f"{path}.{name}")
    elif is_object_sequence(value):
        for index, item in enumerate(value, start=1):
            _warn_extras(item, f"{path}[{index}]")
    elif is_json_object(value):
        for key, item in value.items():
            _warn_extras(item, f"{path}.{key}")
