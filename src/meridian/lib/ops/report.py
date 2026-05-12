"""Report operations for spawn-scoped report files."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.util import FormatContext
from meridian.lib.ops.runtime import (
    async_from_sync,
    resolve_runtime_root_for_read,
    runtime_context,
)
from meridian.lib.ops.spawn.query import resolve_spawn_reference
from meridian.lib.state import spawn_store


def _resolve_target_spawn_id(
    project_root: Path,
    spawn_id: str | None,
    *,
    current_spawn_id: str | None = None,
) -> str:
    candidate = (spawn_id or "").strip()
    if candidate:
        return resolve_spawn_reference(project_root, candidate)
    normalized_current_spawn = (current_spawn_id or "").strip()
    if normalized_current_spawn:
        return resolve_spawn_reference(project_root, normalized_current_spawn)
    raise ValueError("Spawn ID is required. Pass --spawn or set MERIDIAN_SPAWN_ID.")


def _resolve_spawn(
    *,
    project_root: Path,
    spawn_id: str | None,
    ctx: RuntimeContext | None = None,
) -> str:
    resolved_ctx = runtime_context(ctx)
    resolved_spawn = _resolve_target_spawn_id(
        project_root,
        spawn_id,
        current_spawn_id=str(resolved_ctx.spawn_id or ""),
    )
    runtime_root = resolve_runtime_root_for_read(project_root)
    if spawn_store.get_spawn(runtime_root, resolved_spawn) is None:
        raise ValueError(f"Spawn '{resolved_spawn}' not found")
    return resolved_spawn


def _report_path(project_root: Path, *, spawn_id: str) -> Path:
    return resolve_runtime_root_for_read(project_root) / "spawns" / spawn_id / "report.md"


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
    project_root: str | None = None


class ReportShowOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: str
    report_path: str
    report: str

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
    report_path: str
    snippet: str


class ReportSearchOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    results: tuple[ReportSearchResult, ...]

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        if not self.results:
            return "(no matching reports)"
        from meridian.lib.core.formatting import tabular

        return tabular([[item.spawn_id, item.report_path, item.snippet] for item in self.results])


def report_show_sync(
    payload: ReportShowInput,
    ctx: RuntimeContext | None = None,
) -> ReportShowOutput:
    explicit_project_root = (
        Path(payload.project_root).expanduser().resolve() if payload.project_root else None
    )
    project_root = resolve_project_root_resolution(explicit_project_root).project_root
    spawn_id = _resolve_spawn(project_root=project_root, spawn_id=payload.spawn_id, ctx=ctx)
    report_path = _report_path(project_root, spawn_id=spawn_id)
    if not report_path.is_file():
        raise ValueError(f"Report for spawn '{spawn_id}' not found")
    report = report_path.read_text(encoding="utf-8", errors="ignore").strip()
    return ReportShowOutput(
        spawn_id=spawn_id,
        report_path=report_path.as_posix(),
        report=report,
    )


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

    if payload.spawn_id is not None and payload.spawn_id.strip():
        spawn_ids = (resolve_spawn_reference(project_root, payload.spawn_id.strip()),)
    elif resolved_ctx.spawn_id is not None:
        spawn_ids = (resolve_spawn_reference(project_root, str(resolved_ctx.spawn_id)),)
    else:
        spawn_ids = tuple(
            row.id
            for row in reversed(
                spawn_store.list_spawns(resolve_runtime_root_for_read(project_root))
            )
        )

    matches: list[ReportSearchResult] = []
    for spawn_id in spawn_ids:
        report_path = _report_path(project_root, spawn_id=spawn_id)
        if not report_path.is_file():
            continue
        report = report_path.read_text(encoding="utf-8", errors="ignore")
        if query and query.lower() not in report.lower():
            continue
        matches.append(
            ReportSearchResult(
                spawn_id=spawn_id,
                report_path=report_path.as_posix(),
                snippet=_report_snippet(report, query=query),
            )
        )
        if len(matches) >= limit:
            break

    return ReportSearchOutput(results=tuple(matches))


report_show = async_from_sync(report_show_sync)
report_search = async_from_sync(report_search_sync)
