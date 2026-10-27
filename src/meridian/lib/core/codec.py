"""Input/schema helpers shared by typed app surfaces."""

import types
from typing import Any, TypeGuard, TypeVar, Union, cast, get_args, get_origin

from pydantic import BaseModel

PayloadT = TypeVar("PayloadT")


def normalize_optional(annotation: Any) -> tuple[Any, bool]:
    """Return the wrapped type + whether the annotation is Optional[T]."""

    origin = get_origin(annotation)
    if origin is None:
        return annotation, False
    args = get_args(annotation)
    if origin is types.UnionType or origin is Union:
        non_none_args = [arg for arg in args if arg is not type(None)]
        if len(non_none_args) == 1 and len(non_none_args) != len(args):
            return non_none_args[0], True
    return annotation, False


def schema_from_annotation(annotation: Any) -> dict[str, object]:
    normalized, optional = normalize_optional(annotation)
    origin = get_origin(normalized)
    args = get_args(normalized)

    schema: dict[str, object]
    if normalized is str:
        schema = {"type": "string"}
    elif normalized is int:
        schema = {"type": "integer"}
    elif normalized is float:
        schema = {"type": "number"}
    elif normalized is bool:
        schema = {"type": "boolean"}
    elif origin in {list, tuple} and args:
        schema = {"type": "array", "items": schema_from_annotation(args[0])}
    elif isinstance(normalized, type) and _is_pydantic_model_type(cast("object", normalized)):
        schema = schema_from_type(cast("type[Any]", normalized))
    else:
        schema = {"type": "string"}

    if optional:
        return {"anyOf": [schema, {"type": "null"}]}
    return schema


def schema_from_type(payload_type: type[Any]) -> dict[str, object]:
    """Build a basic JSON schema from a Pydantic model."""

    if _is_pydantic_model_type(payload_type):
        return cast("dict[str, object]", payload_type.model_json_schema())
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _is_pydantic_model_type(payload_type: object) -> TypeGuard[type[BaseModel]]:
    return isinstance(payload_type, type) and issubclass(payload_type, BaseModel)
