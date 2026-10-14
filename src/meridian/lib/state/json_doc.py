"""Crash-safe JSON document persistence for small state sidecars."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from meridian.lib.state.atomic import atomic_write_text

ModelT = TypeVar("ModelT", bound=BaseModel)
ResultT = TypeVar("ResultT")


def read_model(path: Path, model_type: type[ModelT]) -> ModelT | None:
    """Read and validate one JSON document, tolerating missing or corrupt state."""

    try:
        return model_type.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None


def write_model(
    path: Path,
    model: BaseModel,
    *,
    indent: int | None = None,
    exclude_none: bool = False,
) -> None:
    """Atomically replace one JSON document with runtime-state permissions."""

    atomic_write_text(
        path,
        model.model_dump_json(indent=indent, exclude_none=exclude_none) + "\n",
    )


def mutate_model(
    path: Path,
    model_type: type[ModelT],
    mutation: Callable[[ModelT | None], tuple[ModelT | None, ResultT]],
    *,
    lock: AbstractContextManager[object],
    indent: int | None = None,
    exclude_none: bool = False,
) -> tuple[ModelT | None, ResultT]:
    """Lock, re-read, mutate, and atomically replace one JSON document."""

    with lock:
        updated, result = mutation(read_model(path, model_type))
        if updated is not None:
            write_model(path, updated, indent=indent, exclude_none=exclude_none)
        return updated, result


__all__ = ["mutate_model", "read_model", "write_model"]
