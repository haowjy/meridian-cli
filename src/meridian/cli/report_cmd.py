"""CLI command handlers for report.* operations."""

from collections.abc import Callable
from functools import partial
from typing import Annotated, Any

from cyclopts import Parameter

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.lib.extensions.registry import get_first_party_registry
from meridian.lib.ops.report import (
    ReportSearchInput,
    ReportShowInput,
    report_search_sync,
    report_show_sync,
)

Emitter = Callable[[Any], None]


def _report_show(
    emit: Emitter,
    spawn: Annotated[
        str | None,
        Parameter(name="--spawn", help="Spawn id or reference (e.g. @latest)."),
    ] = None,
) -> None:
    emit(
        report_show_sync(
            ReportShowInput(
                spawn_id=spawn,
            )
        )
    )


def _report_search(
    emit: Emitter,
    query: Annotated[
        str,
        Parameter(help="Case-insensitive text query."),
    ] = "",
    spawn: Annotated[
        str | None,
        Parameter(name="--spawn", help="Optional spawn id/reference to scope the search."),
    ] = None,
    limit: Annotated[
        int,
        Parameter(name="--limit", help="Maximum number of matching reports to return."),
    ] = 20,
) -> None:
    emit(
        report_search_sync(
            ReportSearchInput(
                query=query,
                spawn_id=spawn,
                limit=limit,
            )
        )
    )


def register_report_commands(app: Any, emit: Emitter) -> tuple[set[str], dict[str, str]]:
    handlers: dict[str, Callable[[], Callable[..., None]]] = {
        "meridian.report.show": lambda: partial(_report_show, emit),
        "meridian.report.search": lambda: partial(_report_search, emit),
    }
    return register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="report",
        handlers=handlers,
        command_help_epilogues={
            "meridian.report.show": ("Example:\n\n  meridian spawn report show p107\n"),
            "meridian.report.search": ('Example:\n\n  meridian spawn report search "auth bug"\n'),
        },
        emit=emit,
    )
