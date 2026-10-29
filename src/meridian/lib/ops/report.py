"""Report operations for spawn-scoped report files."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.locate import resolve_spawn_target
from meridian.lib.ops.runtime import (
    async_from_sync,
    resolve_runtime_root_for_read,
    runtime_context,
)
from meridian.lib.ops.spawn.owed import consume_result_if_shown
from meridian.lib.state.history_index import indexed_spawn_scan
from meridian.lib.state.paths import LaunchPaths, spawn_report_files
from meridian.lib.state.spawn_status import read_derived


def _resolve_target_spawn_id(
    project_root: Path,
    spawn_id: str | None,
    *,
    current_spawn_id: str | None = None,
) -> str:
    candidate = (spawn_id or "").strip()
    if candidate:
        return resolve_spawn_target(project_root, candidate)
    normalized_current_spawn = (current_spawn_id or "").strip()
    if normalized_current_spawn:
        return resolve_spawn_target(project_root, normalized_current_spawn)
    raise ValueError("Spawn ID is required. Pass --spawn or set MERIDIAN_SPAWN_ID.")


def _resolve_spawn(
    *,
    project_root: Path,
    spawn_id: str | None,
    ctx: RuntimeContext | None = None,
) -> tuple[str, str, LaunchPaths]:
    resolved_ctx = runtime_context(ctx)
    resolved_spawn = _resolve_target_spawn_id(
        project_root,
        spawn_id,
        current_spawn_id=str(resolved_ctx.spawn_id or ""),
    )
    runtime_root = resolve_runtime_root_for_read(project_root)
    if runtime_root is None:
        raise ValueError(f"Spawn '{resolved_spawn}' not found")
    row = read_derived(runtime_root, resolved_spawn)
    if row is None:
        raise ValueError(f"Spawn '{resolved_spawn}' not found")
    return resolved_spawn, row.status, LaunchPaths.for_row(runtime_root, row)


def _report_snippet(text: str, *, query: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    if not query:
        return normalized[:200]
    lower_text = normalized.lower()
    lower_query = query.lower()
    idx = lower_text.find(lower_query)
    if idx < 0:
        return normalized[:200]
    start = max(idx - 80, 0)
    end = min(idx + len(query) + 80, len(normalized))
    return normalized[start:end]


class ReportShowInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str | None = None
    observe: bool = False
    project_root: str | None = None


class ReportShowOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    status: str
    report: str
    report_path: str
    """Deprecated: the current run's report file, kept in JSON for one release. The path is an
    internal layout and goes in the next minor release."""

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return self.report


class ReportSearchInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = ""
    spawn_id: str | None = None
    limit: int = 20
    project_root: str | None = None


class ReportSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    snippet: str
    report_path: str
    """Deprecated: the file this hit came from, kept in JSON for one release. The path is an
    internal layout and goes in the next minor release."""


class ReportSearchOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    results: tuple[ReportSearchResult, ...]

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.results:
            return "(no matching reports)"
        from meridian.lib.core.formatting import tabular

        return tabular([[item.spawn_id, item.snippet] for item in self.results])


def report_show_sync(
    payload: ReportShowInput,
    ctx: RuntimeContext | None = None,
) -> ReportShowOutput:
    explicit_project_root = (
        Path(payload.project_root).expanduser().resolve() if payload.project_root else None
    )
    project_root = resolve_project_root_resolution(explicit_project_root).project_root
    resolved_ctx = runtime_context(ctx)
    spawn_id, status, launch = _resolve_spawn(
        project_root=project_root,
        spawn_id=payload.spawn_id,
        ctx=resolved_ctx,
    )
    report_path = launch.report
    if not report_path.is_file():
        raise ValueError(f"Report for spawn '{spawn_id}' not found")
    report = report_path.read_text(encoding="utf-8", errors="ignore").strip()
    result = ReportShowOutput(
        spawn_id=spawn_id,
        status=status,
        report=report,
        report_path=report_path.as_posix(),
    )
    consume_result_if_shown(
        observe=payload.observe,
        runtime_root=launch.runtime_root,
        caller=resolved_ctx,
        spawn_id=spawn_id,
        launch_number=launch.n or 1,
        status=status,
        body=report,
    )
    return result


def report_search_sync(
    payload: ReportSearchInput,
    ctx: RuntimeContext | None = None,
) -> ReportSearchOutput:
    resolved_ctx = runtime_context(ctx)
    explicit_project_root = (
        Path(payload.project_root).expanduser().resolve() if payload.project_root else None
    )
    project_root = resolve_project_root_resolution(explicit_project_root).project_root
    limit = payload.limit if payload.limit > 0 else 20
    query = payload.query.strip()

    runtime_root = resolve_runtime_root_for_read(project_root)
    explicit_id: str | None = None
    if payload.spawn_id is not None and payload.spawn_id.strip():
        explicit_id = resolve_spawn_target(project_root, payload.spawn_id.strip())
    elif resolved_ctx.spawn_id is not None:
        explicit_id = resolve_spawn_target(project_root, str(resolved_ctx.spawn_id))

    if runtime_root is None:
        if explicit_id is not None:
            raise ValueError(f"Report for spawn '{explicit_id}' not found")
        return ReportSearchOutput(results=())
    if explicit_id is None:
        rows = tuple(reversed(indexed_spawn_scan(runtime_root).records))
    else:
        explicit_row = read_derived(runtime_root, explicit_id)
        rows = () if explicit_row is None else (explicit_row,)

    matches: list[ReportSearchResult] = []
    for row in rows:
        # Every run's report is searchable; a spawn matches once, by its newest matching run.
        for report_path in spawn_report_files(runtime_root, row.id):
            report = report_path.read_text(encoding="utf-8", errors="ignore")
            if query and query.lower() not in report.lower():
                continue
            matches.append(
                ReportSearchResult(
                    spawn_id=row.id,
                    snippet=_report_snippet(report, query=query),
                    report_path=report_path.as_posix(),
                )
            )
            break
        if len(matches) >= limit:
            break

    return ReportSearchOutput(results=tuple(matches))


report_show = async_from_sync(report_show_sync)
report_search = async_from_sync(report_search_sync)
