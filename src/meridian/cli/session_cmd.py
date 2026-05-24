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
    segment: Annotated[
        str | None,
        Parameter(
            name="--segment",
            help=(
                "Segment selector: current | previous | N. "
                "Numeric N is absolute from transcript start (segment 0 is initial)."
            ),
        ),
    ] = None,
    compaction: Annotated[
        int | None,
        Parameter(
            name=["--compaction", "-c"],
            help=(
                "Legacy segment selector (0 = current, 1 = previous). Prefer --segment."
            ),
        ),
    ] = None,
    tail: Annotated[
        list[int] | None,
        Parameter(
            name="--tail",
            consume_multiple=True,
            help=(
                "Tail view. Use `--tail` for last 5 messages, or `--tail N` for last N messages."
            ),
        ),
    ] = None,
    from_ordinal: Annotated[
        int | None,
        Parameter(name="--from", help="Window start (absolute message ordinal)."),
    ] = None,
    before_ordinal: Annotated[
        int | None,
        Parameter(name="--before", help="Window ends before this absolute message ordinal."),
    ] = None,
    around_ordinal: Annotated[
        int | None,
        Parameter(name="--around", help="Window centered on this absolute message ordinal."),
    ] = None,
    limit: Annotated[
        int | None,
        Parameter(name="--limit", help="Window size for --from/--before."),
    ] = None,
    context: Annotated[
        int | None,
        Parameter(name="--context", help="Messages on each side for --around."),
    ] = None,
    last_n: Annotated[
        int | None,
        Parameter(
            name=["--last", "-n"],
            help=(
                "Legacy page size inside selected segment. Use --tail / --limit instead. "
                "Use -n 0 for all."
            ),
        ),
    ] = None,
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
    resolved_tail: int | None = None
    if tail is not None:
        if len(tail) > 1:
            raise ValueError("--tail accepts at most one value.")
        resolved_tail = 5 if len(tail) == 0 else tail[0]
    emit(
        session_log_sync(
            SessionLogInput(
                ref=ref,
                segment=segment,
                compaction=compaction,
                tail=resolved_tail,
                from_ordinal=from_ordinal,
                before_ordinal=before_ordinal,
                around_ordinal=around_ordinal,
                limit=limit,
                context=context,
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
            help=(
                "Optional session reference. If omitted, searches across the selected corpus."
            )
        ),
    ] = "",
    file_path: Annotated[
        str | None,
        Parameter(
            name="--file",
            help="Read this session JSONL file directly instead of resolving REF.",
        ),
    ] = None,
    work_id: Annotated[
        str | None,
        Parameter(
            name="--work",
            help="Search sessions attached to this work item (historical associations included).",
        ),
    ] = None,
    workspace: Annotated[
        bool,
        Parameter(name="--workspace", help="Search current project plus workspace roots."),
    ] = False,
    global_scope: Annotated[
        bool,
        Parameter(name="--global", help="Search all local Meridian runtime roots."),
    ] = False,
) -> None:
    emit(
        session_search_sync(
            SessionSearchInput(
                query=query,
                ref=ref,
                file_path=file_path,
                work_id=work_id,
                workspace=workspace,
                global_scope=global_scope,
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
                "  meridian session log c123 --tail\n\n"
                "  meridian session log c123 --tail 20\n\n"
                "  meridian session log c123 --from 120 --limit 30\n\n"
                "  meridian session log c123 --around 240 --context 8\n\n"
                "  meridian session log c123 --segment previous\n\n"
                "Legacy compatibility:\n\n"
                "  meridian session log c123 -c 1 --offset 10 --last 20\n"
            ),
            "meridian.session.export": (
                "Examples:\n\n"
                "  meridian session export c123 > transcript.md\n\n"
                "  meridian session export p107 --include-spawns > transcript.md\n"
            ),
            "meridian.session.search": (
                "Examples:\n\n"
                '  meridian session search "auth bug" c123\n\n'
                '  meridian session search "timeout" --workspace\n\n'
                '  meridian session search "report" --work feature/api-audit\n\n'
                "Search is case-insensitive. Each match includes a deterministic Open command.\n"
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
