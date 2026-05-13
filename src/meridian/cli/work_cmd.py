"""CLI command handlers for work.* operations."""

from collections.abc import Callable
from functools import partial
from typing import Annotated, Any

from cyclopts import App, Parameter

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.lib.core.context import RuntimeContext
from meridian.lib.extensions.registry import get_first_party_registry
from meridian.lib.ops.context import (
    WorkCurrentInput,
    WorkRootInput,
    work_current_sync,
    work_root_sync,
)
from meridian.lib.ops.work_dashboard import (
    WorkDashboardInput,
    WorkListInput,
    WorkSessionsInput,
    WorkShowInput,
    work_dashboard_sync,
    work_list_sync,
    work_sessions_sync,
    work_show_sync,
)
from meridian.lib.ops.work_lifecycle import (
    WorkClearInput,
    WorkDeleteInput,
    WorkDeleteOutput,
    WorkDoneInput,
    WorkRenameInput,
    WorkReopenInput,
    WorkStartInput,
    WorkSwitchInput,
    WorkUpdateInput,
    work_clear_sync,
    work_delete_sync,
    work_done_sync,
    work_rename_sync,
    work_reopen_sync,
    work_start_sync,
    work_switch_sync,
    work_update_sync,
)

Emitter = Callable[[Any], None]


def _runtime_chat_id() -> str:
    return RuntimeContext.from_environment().chat_id


def _work_dashboard(emit: Emitter) -> None:
    emit(work_dashboard_sync(WorkDashboardInput()))


def _work_start(
    emit: Emitter,
    label: Annotated[
        str,
        Parameter(help="Label used to derive the work item slug."),
    ],
    description: Annotated[
        str,
        Parameter(name=["--description", "--desc"], help="Optional work item description."),
    ] = "",
    goal: Annotated[
        str | None,
        Parameter(name="--goal", help="Optional overarching work goal."),
    ] = None,
    worktree: Annotated[
        bool | None,
        Parameter(name="--worktree", help="Create a git worktree for this work item."),
    ] = None,
    no_worktree: Annotated[
        bool,
        Parameter(name="--no-worktree", help="Skip worktree creation even if default is set."),
    ] = False,
) -> None:
    # Resolve explicit worktree intent from the two flags.
    # --no-worktree wins over --worktree if both are passed (degenerate case).
    worktree_intent: bool | None
    if no_worktree:
        worktree_intent = False
    elif worktree:
        worktree_intent = True
    else:
        worktree_intent = None  # defer to config default
    emit(
        work_start_sync(
            WorkStartInput(
                label=label,
                description=description,
                goal=goal,
                chat_id=_runtime_chat_id(),
                worktree=worktree_intent,
            )
        )
    )


def _work_list(
    emit: Emitter,
    done: Annotated[
        bool,
        Parameter(name="--done", help="Show only done/archived items."),
    ] = False,
    limit: Annotated[
        int,
        Parameter(
            name=["-n", "--limit"],
            help="Maximum archived items to show with --done.",
        ),
    ] = 10,
    all_archived: Annotated[
        bool,
        Parameter(name="--all", help="Show all archived items with --done."),
    ] = False,
) -> None:
    emit(
        work_list_sync(
            WorkListInput(
                done_only=done,
                limit=limit,
                all_archived=all_archived,
            )
        )
    )


def _work_show(
    emit: Emitter,
    work_id: Annotated[
        str,
        Parameter(help="Work item id."),
    ],
) -> None:
    emit(work_show_sync(WorkShowInput(work_id=work_id)))


def _work_sessions(
    emit: Emitter,
    work_id: Annotated[
        str,
        Parameter(
            help=(
                "Work item id. Defaults to the active work item "
                "attached to this session (via MERIDIAN_CHAT_ID)."
            )
        ),
    ] = "",
    all: Annotated[
        bool,
        Parameter(name="--all", help="Include historical sessions."),
    ] = False,
) -> None:
    emit(work_sessions_sync(WorkSessionsInput(work_id=work_id, all=all)))


def _work_update(
    emit: Emitter,
    work_id: Annotated[
        str,
        Parameter(help="Work item id."),
    ],
    status: Annotated[
        str | None,
        Parameter(name="--status", help="New work status label."),
    ] = None,
    description: Annotated[
        str | None,
        Parameter(name=["--description", "--desc"], help="Updated work item description."),
    ] = None,
    goal: Annotated[
        str | None,
        Parameter(name="--goal", help="Updated work item goal."),
    ] = None,
) -> None:
    emit(
        work_update_sync(
            WorkUpdateInput(
                work_id=work_id,
                status=status,
                description=description,
                goal=goal,
            )
        )
    )


def _work_done(
    emit: Emitter,
    work_id: Annotated[
        str,
        Parameter(help="Work item id."),
    ],
    force: Annotated[
        bool,
        Parameter(
            name="--force",
            help="Remove worktree even if the branch has unpushed commits.",
        ),
    ] = False,
) -> None:
    emit(work_done_sync(WorkDoneInput(work_id=work_id, force=force)))


def _work_delete(
    emit: Emitter,
    work_id: Annotated[
        str,
        Parameter(help="Work item id."),
    ],
    force: Annotated[
        bool,
        Parameter(name="--force", help="Delete even if work item has artifacts."),
    ] = False,
) -> None:
    output: WorkDeleteOutput = work_delete_sync(WorkDeleteInput(work_id=work_id, force=force))
    emit(output)


def _work_switch(
    emit: Emitter,
    work_id: Annotated[
        str,
        Parameter(help="Work item id."),
    ],
) -> None:
    emit(work_switch_sync(WorkSwitchInput(work_id=work_id, chat_id=_runtime_chat_id())))


def _work_reopen(
    emit: Emitter,
    work_id: Annotated[
        str,
        Parameter(help="Work item id."),
    ],
) -> None:
    emit(work_reopen_sync(WorkReopenInput(work_id=work_id)))


def _work_rename(
    emit: Emitter,
    work_id: Annotated[
        str,
        Parameter(help="Current work item id."),
    ],
    new_name: Annotated[
        str,
        Parameter(help="New name (slug) for the work item."),
    ],
    worktree: Annotated[
        bool,
        Parameter(name="--worktree", help="Also move the git worktree and rename its branch."),
    ] = False,
) -> None:
    emit(
        work_rename_sync(
            WorkRenameInput(
                work_id=work_id,
                new_name=new_name,
                rename_worktree=worktree,
                chat_id=_runtime_chat_id(),
            )
        )
    )


def _work_clear(emit: Emitter) -> None:
    emit(work_clear_sync(WorkClearInput(chat_id=_runtime_chat_id())))


def _work_current(emit: Emitter) -> None:
    emit(work_current_sync(WorkCurrentInput()))


def _work_root(emit: Emitter) -> None:
    emit(work_root_sync(WorkRootInput()))


def register_work_commands(app: App, emit: Emitter) -> tuple[set[str], dict[str, str]]:
    """Register work CLI commands using registry metadata as source of truth."""

    handlers: dict[str, Callable[[], Callable[..., None]]] = {
        "meridian.work.current": lambda: partial(_work_current, emit),
        "meridian.work.root": lambda: partial(_work_root, emit),
        "meridian.work.start": lambda: partial(_work_start, emit),
        "meridian.work.list": lambda: partial(_work_list, emit),
        "meridian.work.show": lambda: partial(_work_show, emit),
        "meridian.work.sessions": lambda: partial(_work_sessions, emit),
        "meridian.work.update": lambda: partial(_work_update, emit),
        "meridian.work.done": lambda: partial(_work_done, emit),
        "meridian.work.delete": lambda: partial(_work_delete, emit),
        "meridian.work.switch": lambda: partial(_work_switch, emit),
        "meridian.work.reopen": lambda: partial(_work_reopen, emit),
        "meridian.work.rename": lambda: partial(_work_rename, emit),
        "meridian.work.clear": lambda: partial(_work_clear, emit),
    }
    return register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="work",
        handlers=handlers,
        emit=emit,
        default_handler=partial(_work_dashboard, emit),
    )
