"""Context query operations — runtime context derivation via CLI query."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.config.context_config import ArbitraryContextConfig, ContextConfig
from meridian.lib.context.resolver import (
    ResolvedContextPaths,
    render_context_lines,
    resolve_context_paths,
)
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.util import FormatContext
from meridian.lib.hooks.builtin.autosync_store import read_status
from meridian.lib.launch.cwd import resolve_effective_task_dir
from meridian.lib.ops.depth import with_record_backed_depth
from meridian.lib.ops.runtime import (
    resolve_runtime_authority_for_read,
    resolve_runtime_authority_for_write,
)
from meridian.lib.state.paths import load_context_config
from meridian.lib.state.spawn_scope import write_spawn_scope_task_dir
from meridian.lib.state.work_scope import WorkScope


class ContextInput(BaseModel):
    """Input for context query operation."""

    model_config = ConfigDict(frozen=True)
    verbose: bool = False


class ContextEntryOutput(BaseModel):
    """Output for one named context entry."""

    model_config = ConfigDict(frozen=True)

    source: str
    path: str
    resolved: str


class ContextOutput(BaseModel):
    """Output for context query operation."""

    model_config = ConfigDict(frozen=True)

    work_path: str
    work_resolved: str
    work_source: str
    active_work_dir: str | None = None
    work_archive: str
    work_archive_resolved: str
    kb_path: str
    kb_resolved: str
    kb_source: str
    extra_contexts: dict[str, ContextEntryOutput] = Field(default_factory=dict)
    sync_status: dict[str, str] = Field(default_factory=dict)
    render_verbose: bool = Field(default=False, exclude=True, repr=False)

    def _available_names(self) -> tuple[str, ...]:
        return ("work", "kb", "work.archive", *sorted(self.extra_contexts))

    def _to_resolved_paths(self) -> ResolvedContextPaths:
        """Convert output fields back to ResolvedContextPaths for rendering."""

        from meridian.lib.config.context_config import ContextSourceType

        extra: dict[str, tuple[Path, ContextSourceType]] = {}
        for name, entry in self.extra_contexts.items():
            extra[name] = (Path(entry.resolved), ContextSourceType(entry.source))
        return ResolvedContextPaths(
            work_root=Path(self.work_resolved),
            work_archive=Path(self.work_archive_resolved),
            work_source=ContextSourceType(self.work_source),
            kb_root=Path(self.kb_resolved),
            kb_source=ContextSourceType(self.kb_source),
            extra=extra,
        )

    def format_text(self, ctx: FormatContext | None = None) -> str:
        verbose = self.render_verbose
        if ctx is not None and ctx.verbosity > 0:
            verbose = True

        lines: list[str]
        if verbose:
            lines = []
            lines.append("work:")
            lines.append(f"  source: {self.work_source}")
            lines.append(f"  path: {self.work_path}")
            lines.append(f"  resolved: {self.work_resolved}")
            lines.append(f"  active: {self.active_work_dir or '(none)'}")
            lines.append(f"  archive: {self.work_archive}")
            lines.append(f"  archive_resolved: {self.work_archive_resolved}")
            lines.append("kb:")
            lines.append(f"  source: {self.kb_source}")
            lines.append(f"  path: {self.kb_path}")
            lines.append(f"  resolved: {self.kb_resolved}")
            for name in sorted(self.extra_contexts):
                entry = self.extra_contexts[name]
                lines.append(f"{name}:")
                lines.append(f"  source: {entry.source}")
                lines.append(f"  path: {entry.path}")
                lines.append(f"  resolved: {entry.resolved}")
        else:
            active_work_dir = Path(self.active_work_dir) if self.active_work_dir else None
            lines = list(
                render_context_lines(
                    self._to_resolved_paths(),
                    check_env=True,
                    active_work_dir=active_work_dir,
                )
            )

        if self.sync_status:
            lines.append("")
            lines.append("Sync:")
            for name in sorted(self.sync_status):
                status_lines = self.sync_status[name].splitlines() or [""]
                lines.append(f"  {name}: {status_lines[0]}")
                for status_line in status_lines[1:]:
                    lines.append(status_line)

        return "\n".join(lines)

    def resolve_name(self, name: str) -> str:
        """Resolve one context-name query to its absolute path string."""

        normalized = name.strip().lower()
        if normalized == "work":
            return self.active_work_dir or ""
        if normalized == "kb":
            return self.kb_resolved
        if normalized in {"work.archive", "archive", "archive.work"}:
            return self.work_archive_resolved
        if normalized in self.extra_contexts:
            return self.extra_contexts[normalized].resolved
        raise KeyError(
            f"Unknown context '{name}'. Expected one of: {', '.join(self._available_names())}."
        )


class WorkCurrentInput(BaseModel):
    """Input for work current operation."""

    model_config = ConfigDict(frozen=True)


class WorkCurrentOutput(BaseModel):
    """Output for work current operation."""

    model_config = ConfigDict(frozen=True)

    work_dir: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return self.work_dir or ""


class TaskDirInput(BaseModel):
    """Input for spawn-scope task-dir query."""

    model_config = ConfigDict(frozen=True)


class TaskDirSetInput(BaseModel):
    """Input for spawn-scope task-dir set."""

    model_config = ConfigDict(frozen=True)

    path: str


class TaskDirClearInput(BaseModel):
    """Input for spawn-scope task-dir clear."""

    model_config = ConfigDict(frozen=True)


class TaskDirOutput(BaseModel):
    """Output for spawn-scope task-dir query."""

    model_config = ConfigDict(frozen=True)

    task_dir: str
    source: str
    spawn_id: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return self.task_dir


class TaskDirMutationOutput(BaseModel):
    """Output for spawn-scope task-dir set/clear (no text output)."""

    model_config = ConfigDict(frozen=True)

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return ""


class WorkRootInput(BaseModel):
    """Input for work root operation."""

    model_config = ConfigDict(frozen=True)


class WorkRootOutput(BaseModel):
    """Output for work root operation."""

    model_config = ConfigDict(frozen=True)

    work_root: str

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return self.work_root


class WorkPathInput(BaseModel):
    """Input for work path materialization."""

    model_config = ConfigDict(frozen=True)

    relpath: str


class WorkPathOutput(BaseModel):
    """Output for work path materialization."""

    model_config = ConfigDict(frozen=True)

    path: str

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        return self.path


def _resolve_runtime_context(
    project_root: Path,
    runtime_root: Path,
    *,
    chat_id: str | None = None,
) -> ResolvedContext:
    """Resolve context with explicit roots — no env mutation needed."""

    normalized_chat_id = (chat_id or "").strip()
    resolved = ResolvedContext.from_environment(
        explicit_project_root=project_root,
        explicit_runtime_root=runtime_root,
        explicit_chat_id=normalized_chat_id or None,
    )
    return with_record_backed_depth(resolved)


def resolve_active_work_scope(
    project_root: Path,
    runtime_root: Path,
    *,
    chat_id: str | None = None,
) -> WorkScope | None:
    """Return the active work scope using authoritative context resolution."""

    resolved = _resolve_runtime_context(project_root, runtime_root, chat_id=chat_id)
    return resolved.work_scope


def resolve_active_work_scope_dir(
    project_root: Path,
    runtime_root: Path,
    *,
    chat_id: str | None = None,
) -> Path | None:
    """Return the active work scope directory using the same resolution as work current."""

    scope = resolve_active_work_scope(project_root, runtime_root, chat_id=chat_id)
    return scope.root if scope is not None else None


def _join_scope_path(scope_dir: Path, relpath: str) -> Path:
    """Join a relative path under scope_dir, rejecting escapes."""

    normalized = relpath.strip()
    if not normalized:
        raise ValueError("work path relpath must not be empty.")
    relative = Path(normalized)
    # Reject absolute/rooted/drive paths under either OS convention — `is_absolute()`
    # is platform-dependent (e.g. "/tmp/x" is absolute on POSIX but drive-relative,
    # so non-absolute, on Windows), and Windows is a first-class target.
    if (
        PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(normalized).is_absolute()
        or normalized.startswith(("/", "\\"))
        or PureWindowsPath(normalized).drive != ""
    ):
        raise ValueError(f"work path must be relative, got: {relpath}")
    scope_resolved = scope_dir.resolve()
    target = (scope_dir / relative).resolve()
    try:
        target.relative_to(scope_resolved)
    except ValueError as exc:
        raise ValueError(
            "work path escapes scope directory.\n"
            f"  Resolved: {target}\n"
            f"  Scope:    {scope_resolved}"
        ) from exc
    return target


def _require_session_spawn_id(ctx: RuntimeContext) -> str:
    if ctx.spawn_id is None:
        raise ValueError("Not in a session (MERIDIAN_SPAWN_ID is unset).")
    return str(ctx.spawn_id)


def _validated_task_dir_path(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.is_dir():
        return resolved
    if not resolved.exists():
        raise ValueError(f"task_dir does not exist: {resolved}")
    raise ValueError(f"task_dir is not a directory: {resolved}")


def _resolve_effective_task_dir_output() -> TaskDirOutput:
    authority = resolve_runtime_authority_for_read()
    runtime_ctx = RuntimeContext.from_environment()
    resolved = ResolvedContext.from_environment()
    effective = resolve_effective_task_dir(
        project_root=authority.project_root,
        project_state_dir=authority.project_state_dir,
        spawn_id=str(runtime_ctx.spawn_id) if runtime_ctx.spawn_id is not None else None,
        inherited_task_dir=resolved.inherited_task_dir,
        work_id=resolved.work_id,
    )
    return TaskDirOutput(
        task_dir=effective.task_dir.as_posix(),
        source=effective.source,
        spawn_id=str(runtime_ctx.spawn_id) if runtime_ctx.spawn_id is not None else None,
    )


def task_dir_sync(input: TaskDirInput) -> TaskDirOutput:
    """Synchronous handler for spawn-scope task-dir query."""

    _ = input
    return _resolve_effective_task_dir_output()


def task_dir_set_sync(input: TaskDirSetInput) -> TaskDirMutationOutput:
    """Write spawn-scope task-dir override for the current session."""

    authority = resolve_runtime_authority_for_write()
    runtime_ctx = RuntimeContext.from_environment()
    spawn_id = _require_session_spawn_id(runtime_ctx)
    task_dir = _validated_task_dir_path(input.path)
    write_spawn_scope_task_dir(authority.project_root, spawn_id, task_dir)
    return TaskDirMutationOutput()


def task_dir_clear_sync(input: TaskDirClearInput) -> TaskDirMutationOutput:
    """Tombstone spawn-scope task-dir for the current session."""

    _ = input
    authority = resolve_runtime_authority_for_write()
    runtime_ctx = RuntimeContext.from_environment()
    spawn_id = _require_session_spawn_id(runtime_ctx)
    write_spawn_scope_task_dir(authority.project_root, spawn_id, None)
    return TaskDirMutationOutput()


async def task_dir(input: TaskDirInput) -> TaskDirOutput:
    return await asyncio.to_thread(task_dir_sync, input)


async def task_dir_set(input: TaskDirSetInput) -> TaskDirMutationOutput:
    return await asyncio.to_thread(task_dir_set_sync, input)


async def task_dir_clear(input: TaskDirClearInput) -> TaskDirMutationOutput:
    return await asyncio.to_thread(task_dir_clear_sync, input)


def _extra_context_config(config: ContextConfig) -> dict[str, ArbitraryContextConfig]:
    """Return arbitrary context configs keyed by normalized lookup name."""

    extras_raw = getattr(config, "__pydantic_extra__", None)
    extras = cast("dict[str, object]", extras_raw) if isinstance(extras_raw, dict) else {}
    parsed: dict[str, ArbitraryContextConfig] = {}
    for name, value in extras.items():
        parsed[name.strip().lower()] = (
            value
            if isinstance(value, ArbitraryContextConfig)
            else ArbitraryContextConfig.model_validate(value)
        )
    return parsed


def _relative_time(iso_str: str) -> str:
    """Format an ISO timestamp as a compact relative time string."""

    try:
        normalized = iso_str.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        delta = max(0, int((now - parsed).total_seconds()))
    except (TypeError, ValueError):
        return iso_str

    if delta < 60:
        return f"{delta}s ago"
    minutes = delta // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _sync_status_for_context(context_root: Path) -> str | None:
    """Return compact sync status text for one context root."""
    status = read_status(context_root)
    if status.state is None and not status.unresolved_conflicts:
        return None

    state_info = "unknown"
    if status.state is not None:
        if status.state.last_sync:
            state_info = f"{status.state.outcome}, {_relative_time(status.state.last_sync)}"
        else:
            state_info = status.state.outcome

    if status.unresolved_conflicts:
        conflict_lines: list[str] = []
        for c in status.unresolved_conflicts:
            paths_str = ", ".join(c.paths) or "(no paths)"
            conflict_lines.append(f"    {c.id} {paths_str} ({c.conflict_type})")
        return "\n".join(
            [
                f"conflict ({state_info}) — {len(status.unresolved_conflicts)} unresolved",
                *conflict_lines,
            ]
        )

    return f"ok ({state_info})"


def context_sync(input: ContextInput) -> ContextOutput:
    """Synchronous handler for context query."""

    authority = resolve_runtime_authority_for_read()
    runtime_root = authority.runtime_root or authority.project_state_dir
    resolved_runtime_context = _resolve_runtime_context(authority.project_root, runtime_root)
    context_config = load_context_config(authority.project_root) or ContextConfig()
    resolved_paths = resolve_context_paths(authority.project_root, context_config)
    extra_config = _extra_context_config(context_config)
    extra_contexts: dict[str, ContextEntryOutput] = {}
    sync_status: dict[str, str] = {}

    work_sync = _sync_status_for_context(resolved_paths.work_root)
    if work_sync is not None:
        sync_status["work"] = work_sync
    kb_sync = _sync_status_for_context(resolved_paths.kb_root)
    if kb_sync is not None:
        sync_status["kb"] = kb_sync

    for name, (path, source) in resolved_paths.extra.items():
        normalized = name.strip().lower()
        config_entry = extra_config.get(normalized)
        if config_entry is None:
            continue
        extra_contexts[normalized] = ContextEntryOutput(
            source=source.value,
            path=config_entry.path,
            resolved=path.as_posix(),
        )
        extra_sync = _sync_status_for_context(path)
        if extra_sync is not None:
            sync_status[normalized] = extra_sync

    return ContextOutput(
        work_path=context_config.work.path,
        work_resolved=resolved_paths.work_root.as_posix(),
        work_source=context_config.work.source.value,
        active_work_dir=(
            resolved_runtime_context.work_dir.as_posix()
            if resolved_runtime_context.work_dir is not None
            else None
        ),
        work_archive=context_config.work.archive,
        work_archive_resolved=resolved_paths.work_archive.as_posix(),
        kb_path=context_config.kb.path,
        kb_resolved=resolved_paths.kb_root.as_posix(),
        kb_source=context_config.kb.source.value,
        extra_contexts=extra_contexts,
        sync_status=sync_status,
        render_verbose=input.verbose,
    )


async def context(input: ContextInput) -> ContextOutput:
    """Async handler for context query."""

    return await asyncio.to_thread(context_sync, input)


def work_current_sync(input: WorkCurrentInput) -> WorkCurrentOutput:
    """Synchronous handler for work current query."""

    _ = input
    authority = resolve_runtime_authority_for_read()
    runtime_root = authority.runtime_root or authority.project_state_dir
    scope = resolve_active_work_scope(authority.project_root, runtime_root)

    return WorkCurrentOutput(
        work_dir=scope.root.as_posix() if scope is not None else None
    )


def work_path_sync(input: WorkPathInput) -> WorkPathOutput:
    """Materialize a path under the active work scope and return its absolute path."""

    authority = resolve_runtime_authority_for_read()
    runtime_root = authority.runtime_root or authority.project_state_dir
    scope = resolve_active_work_scope(authority.project_root, runtime_root)
    if scope is None:
        raise ValueError("No active work scope is resolvable for this process.")

    target = _join_scope_path(scope.root, input.relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    return WorkPathOutput(path=target.as_posix())


def work_root_sync(input: WorkRootInput) -> WorkRootOutput:
    """Synchronous handler for work root query."""

    _ = input
    import os

    env_work_root = os.getenv("MERIDIAN_CONTEXT_WORK_DIR", "").strip()
    if env_work_root:
        return WorkRootOutput(work_root=env_work_root)

    authority = resolve_runtime_authority_for_read()
    context_config = load_context_config(authority.project_root) or ContextConfig()
    resolved_paths = resolve_context_paths(authority.project_root, context_config)
    return WorkRootOutput(work_root=resolved_paths.work_root.as_posix())


async def work_current(input: WorkCurrentInput) -> WorkCurrentOutput:
    """Async handler for work current query."""

    return await asyncio.to_thread(work_current_sync, input)


async def work_root(input: WorkRootInput) -> WorkRootOutput:
    """Async handler for work root query."""

    return await asyncio.to_thread(work_root_sync, input)


async def work_path(input: WorkPathInput) -> WorkPathOutput:
    """Async handler for work path materialization."""

    return await asyncio.to_thread(work_path_sync, input)


__all__ = [
    "ContextEntryOutput",
    "ContextInput",
    "ContextOutput",
    "TaskDirClearInput",
    "TaskDirInput",
    "TaskDirMutationOutput",
    "TaskDirOutput",
    "TaskDirSetInput",
    "WorkCurrentInput",
    "WorkCurrentOutput",
    "WorkPathInput",
    "WorkPathOutput",
    "WorkRootInput",
    "WorkRootOutput",
    "context",
    "context_sync",
    "resolve_active_work_scope",
    "resolve_active_work_scope_dir",
    "task_dir",
    "task_dir_clear",
    "task_dir_clear_sync",
    "task_dir_set",
    "task_dir_set_sync",
    "task_dir_sync",
    "work_current",
    "work_current_sync",
    "work_path",
    "work_path_sync",
    "work_root",
    "work_root_sync",
]
