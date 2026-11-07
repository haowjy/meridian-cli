from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict


class FormatContext(BaseModel):
    """Parameters passed to text formatters.

    Provides a stable extension point so format_text() signatures never need
    to change when new formatting knobs are added (verbosity, width, etc.).
    """

    model_config = ConfigDict(frozen=True)

    verbosity: int = 0  # 0=normal, 1=verbose, -1=quiet
    width: int = 80  # terminal column width hint


@runtime_checkable
class TextFormattable(Protocol):
    """Protocol for output models that provide a human-readable text format."""

    def format_text(self, ctx: FormatContext | None = None) -> str: ...


def to_jsonable(value: Any) -> Any:

    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        typed_dict = cast("dict[object, object]", value)
        return {str(key): to_jsonable(item) for key, item in typed_dict.items()}
    if isinstance(value, (list, tuple, set)):
        typed_seq = cast("list[object] | tuple[object, ...] | set[object]", value)
        return [to_jsonable(item) for item in typed_seq]
    return value
