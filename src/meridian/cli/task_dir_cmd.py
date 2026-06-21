"""CLI command handlers for task-dir.* operations."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Annotated, Any

from cyclopts import App, Parameter

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.lib.extensions.registry import get_first_party_registry
from meridian.lib.ops.context import (
    TaskDirClearInput,
    TaskDirInput,
    TaskDirSetInput,
    task_dir_clear_sync,
    task_dir_set_sync,
    task_dir_sync,
)

Emitter = Callable[[Any], None]


def _task_dir_query(emit: Emitter) -> None:
    emit(task_dir_sync(TaskDirInput()))


def _task_dir_set(
    emit: Emitter,
    path: Annotated[
        str,
        Parameter(help="Existing directory to use as the source-edit root for this spawn."),
    ],
) -> None:
    emit(task_dir_set_sync(TaskDirSetInput(path=path)))


def _task_dir_clear(emit: Emitter) -> None:
    emit(task_dir_clear_sync(TaskDirClearInput()))


def register_task_dir_commands(app: App, emit: Emitter) -> tuple[set[str], dict[str, str]]:
    """Register task-dir CLI commands using registry metadata as source of truth."""

    handlers: dict[str, Callable[[], Callable[..., None]]] = {
        "meridian.task-dir.set": lambda: partial(_task_dir_set, emit),
        "meridian.task-dir.clear": lambda: partial(_task_dir_clear, emit),
    }
    return register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="task-dir",
        handlers=handlers,
        emit=emit,
        default_handler=partial(_task_dir_query, emit),
    )
