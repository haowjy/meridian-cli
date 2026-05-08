"""CLI command handlers for session.* operations."""

from collections.abc import Callable
from functools import partial
from typing import Annotated, Any

from cyclopts import App, Parameter

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.lib.extensions.registry import get_first_party_registry
from meridian.lib.ops.session_export import SessionExportInput, session_export_sync
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.ops.session_repair import SessionRepairInput, repair_session_reference_sync
from meridian.lib.ops.session_search import SessionSearchInput, session_search_sync

Emitter = Callable[[Any], None]


def _session_log(
    emit: Emitter,
    ref: Annotated[
        str,
        Parameter(
            help=("Session reference: chat id (c123), spawn id (p123), or harness session id.")
        ),
    ] = "",
    compaction: Annotated[
        int,
        Parameter(
            name=["--compaction", "-c"],
            help=(
                "Compaction segment index (0 = after last boundary, 1 = previous segment, etc.)."
            ),
        ),
    ] = 0,
    last_n: Annotated[
        int | None,
        Parameter(
            name=["--last", "-n"],
            help=(
                "Number of messages to show inside the selected segment "
                "(default: 5; use -n 0 for all)."
            ),
        ),
    ] = 5,
    offset: Annotated[
        int,
        Parameter(
            name="--offset",
            help="Skip this many messages from the end of the selected segment.",
        ),
    ] = 0,
    file_path: Annotated[
        str | None,
        Parameter(
            name="--file",
            help="Read this session JSONL file directly instead of resolving REF.",
        ),
    ] = None,
) -> None:
    mapped_last_n: int | None = None if last_n == 0 else last_n
    emit(
        session_log_sync(
            SessionLogInput(
                ref=ref,
                compaction=compaction,
                last_n=mapped_last_n,
                offset=offset,
                file_path=file_path,
            )
        )
    )


def _session_export(
    emit: Emitter,
    ref: Annotated[
        str,
        Parameter(
            help=("Session reference: chat id (c123), spawn id (p123), or harness session id.")
        ),
    ] = "",
    file_path: Annotated[
        str | None,
        Parameter(
            name="--file",
            help="Read this session JSONL file directly instead of resolving REF.",
        ),
    ] = None,
    include_spawns: Annotated[
        bool,
        Parameter(
            name="--include-spawns",
            help="Append terminal child-spawn reports as markdown appendices.",
        ),
    ] = False,
) -> None:
    emit(
        session_export_sync(
            SessionExportInput(
                ref=ref,
                file_path=file_path,
                include_spawns=include_spawns,
            )
        )
    )


def _session_search(
    emit: Emitter,
    query: Annotated[
        str,
        Parameter(help="Case-insensitive text query."),
    ],
    ref: Annotated[
        str,
        Parameter(
            help=("Session reference: chat id (c123), spawn id (p123), or harness session id.")
        ),
    ],
    file_path: Annotated[
        str | None,
        Parameter(
            name="--file",
            help="Read this session JSONL file directly instead of resolving REF.",
        ),
    ] = None,
) -> None:
    emit(
        session_search_sync(
            SessionSearchInput(
                query=query,
                ref=ref,
                file_path=file_path,
            )
        )
    )


def _session_repair(
    emit: Emitter,
    ref: Annotated[
        str,
        Parameter(
            help=("Session reference: chat id (c123), spawn id (p123), or harness session id.")
        ),
    ],
) -> None:
    emit(
        repair_session_reference_sync(
            SessionRepairInput(
                ref=ref,
            )
        )
    )


def register_session_commands(app: App, emit: Emitter) -> tuple[set[str], dict[str, str]]:
    """Register session CLI commands using registry metadata as source of truth."""

    handlers: dict[str, Callable[[], Callable[..., None]]] = {
        "meridian.session.log": lambda: partial(_session_log, emit),
        "meridian.session.export": lambda: partial(_session_export, emit),
        "meridian.session.search": lambda: partial(_session_search, emit),
        "meridian.session.repair": lambda: partial(_session_repair, emit),
    }
    return register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="session",
        handlers=handlers,
        command_help_epilogues={
            "meridian.session.log": (
                "Examples:\n\n"
                "  meridian session log c123\n\n"
                "  meridian session log c123 -n 20\n\n"
                "  meridian session log p107 --last 20\n\n"
                "  meridian session log c123 -c 0 -n 0    # latest segment, all messages\n\n"
                "  meridian session log c123 -c 2          # older segment "
                "(higher numbers walk backward)\n"
            ),
            "meridian.session.export": (
                "Examples:\n\n"
                "  meridian session export c123 > transcript.md\n\n"
                "  meridian session export p107 --include-spawns > transcript.md\n"
            ),
            "meridian.session.search": (
                "Example:\n\n"
                "  meridian session search \"auth bug\" c123\n\n"
                "Search is case-insensitive. Output includes navigation hints, for example:\n\n"
                "  Navigate: meridian session log c123 -c 0 --offset 37 --last 10\n"
            ),
            "meridian.session.repair": (
                "Examples:\n\n"
                "  meridian session repair c123\n\n"
                "  meridian session repair p107\n\n"
                "Repair is explicit and opt-in. Normal session reads do not mutate state.\n"
            ),
        },
        emit=emit,
        default_handler=None,
    )
