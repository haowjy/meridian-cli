"""CLI command handlers for telemetry reader operations."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from cyclopts import App, Parameter

from meridian.cli.utils import require_established_project_root
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state.user_paths import get_user_home
from meridian.lib.telemetry.query import query_events
from meridian.lib.telemetry.reader import snapshot_segment_offsets, tail_events
from meridian.lib.telemetry.status import (
    ROOTLESS_LIMITATION_NOTE,
    compute_status,
    format_status_dict,
    status_to_dict,
)

Emitter = Callable[[Any], None]


class _TelemetryStatusResult(dict[str, Any]):
    """Dict payload that keeps human-readable text formatting."""

    def format_text(self, ctx: Any = None) -> str:
        _ = ctx
        return format_status_dict(self)


def _resolve_telemetry_dirs(global_flag: bool) -> list[Path]:
    """Return telemetry directories to read."""
    if global_flag:
        projects_dir = get_user_home() / "projects"
        dirs: list[Path] = []
        if projects_dir.is_dir():
            for directory in sorted(projects_dir.iterdir()):
                telemetry_dir = directory / "telemetry"
                if directory.is_dir() and telemetry_dir.is_dir():
                    dirs.append(telemetry_dir)
        legacy = _legacy_telemetry_dir()
        if legacy is not None:
            dirs.append(legacy)
        return dirs

    project_root = require_established_project_root()
    try:
        runtime_root = resolve_runtime_root_for_read(project_root)
    except Exception as exc:
        raise ValueError(
            "Not inside a Meridian project. Use --global to query across all projects."
        ) from exc

    telemetry_dir = runtime_root / "telemetry"
    return [telemetry_dir] if telemetry_dir.is_dir() else []


def _legacy_telemetry_dir() -> Path | None:
    """Return legacy user-level telemetry dir if it exists."""
    legacy = get_user_home() / "telemetry"
    return legacy if legacy.is_dir() else None


def _ids_filter(
    *,
    spawn_id: str = "",
    chat_id: str = "",
    work_id: str = "",
) -> dict[str, str] | None:
    filters: dict[str, str] = {}
    if spawn_id:
        filters["spawn_id"] = spawn_id
    if chat_id:
        filters["chat_id"] = chat_id
    if work_id:
        filters["work_id"] = work_id
    return filters or None


def _write_event(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _telemetry_tail(
    domain: Annotated[
        str | None,
        Parameter(name="--domain", help="Filter by telemetry domain."),
    ] = None,
    spawn_id: Annotated[
        str,
        Parameter(name="--spawn", help="Filter by spawn id."),
    ] = "",
    chat_id: Annotated[
        str,
        Parameter(name="--chat", help="Filter by chat id."),
    ] = "",
    work_id: Annotated[
        str,
        Parameter(name="--work", help="Filter by work id."),
    ] = "",
    global_flag: Annotated[
        bool,
        Parameter(name="--global", help="Stream from all projects."),
    ] = False,
) -> None:
    """Live stream local telemetry events.

    Rootless MCP stdio server processes write telemetry to stderr only and are not
    visible in local segment readers.
    """
    dirs = _resolve_telemetry_dirs(global_flag)
    if not dirs:
        return
    try:
        offsets = snapshot_segment_offsets(dirs)
        for event in tail_events(
            dirs,
            start_offsets=offsets,
            domain=domain,
            ids_filter=_ids_filter(spawn_id=spawn_id, chat_id=chat_id, work_id=work_id),
        ):
            _write_event(event)
    except KeyboardInterrupt:
        return


def _telemetry_query(
    since: Annotated[
        str | None,
        Parameter(name="--since", help="Only include events newer than duration (for example 1h)."),
    ] = None,
    domain: Annotated[
        str | None,
        Parameter(name="--domain", help="Filter by telemetry domain."),
    ] = None,
    spawn_id: Annotated[
        str,
        Parameter(name="--spawn", help="Filter by spawn id."),
    ] = "",
    chat_id: Annotated[
        str,
        Parameter(name="--chat", help="Filter by chat id."),
    ] = "",
    work_id: Annotated[
        str,
        Parameter(name="--work", help="Filter by work id."),
    ] = "",
    limit: Annotated[
        int | None,
        Parameter(name="--limit", help="Maximum events to return."),
    ] = None,
    global_flag: Annotated[
        bool,
        Parameter(name="--global", help="Query across all projects."),
    ] = False,
) -> None:
    """Print historical local telemetry events as JSON lines."""
    dirs = _resolve_telemetry_dirs(global_flag)
    if not dirs:
        return
    for event in query_events(
        dirs,
        since=since,
        domain=domain,
        ids_filter=_ids_filter(spawn_id=spawn_id, chat_id=chat_id, work_id=work_id),
        limit=limit,
    ):
        _write_event(event)


def _telemetry_status(
    emit: Emitter,
    *,
    global_flag: bool = False,
) -> None:
    """Show telemetry sink health and local reader limitations."""
    dirs = _resolve_telemetry_dirs(global_flag)
    if not dirs:
        emit(
            _TelemetryStatusResult(
                {
                    "segment_count": 0,
                    "total_bytes": 0,
                    "active_writers": [],
                    "total_size_human": "0 B",
                    "telemetry_dir": "",
                    "rootless_note": ROOTLESS_LIMITATION_NOTE,
                }
            )
        )
        return

    runtime_root = dirs[0].parent if dirs else get_user_home()
    legacy = _legacy_telemetry_dir() if global_flag else None
    status = compute_status(
        runtime_root,
        telemetry_dirs=dirs if len(dirs) > 1 else None,
        legacy_dir=legacy,
    )
    emit(_TelemetryStatusResult(status_to_dict(status)))


def register_telemetry_commands(app: App, emit: Emitter) -> None:
    """Register telemetry CLI commands."""
    app.command(_telemetry_tail, name="tail")
    app.command(_telemetry_query, name="query")

    def _status_cmd(
        global_flag: Annotated[
            bool,
            Parameter(name="--global", help="Aggregate status across all projects."),
        ] = False,
    ) -> None:
        _telemetry_status(emit, global_flag=global_flag)

    app.command(
        _status_cmd,
        name="status",
        help="Show telemetry sink health and local reader limitations.",
    )
