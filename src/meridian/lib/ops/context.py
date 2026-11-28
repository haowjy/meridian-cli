from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.autosync.clones import require_outside_clone
from meridian.lib.catalog.mars_roots import mars_fallback_label
from meridian.lib.config.context_config import ContextConfig
from meridian.lib.context.resolved import ResolvedContext
from meridian.lib.context.resolver import (
    ResolvedContext as ResolvedContextEntry,
)
from meridian.lib.context.resolver import (
    ResolvedContextPaths,
    context_env_key,
    resolve_context_paths,
)
from meridian.lib.context.runtime import RuntimeContext
from meridian.lib.core.refs import RefNotFound
from meridian.lib.core.util import FormatContext
from meridian.lib.launch.cwd import inheritable_task_dir, resolve_effective_task_dir
from meridian.lib.launch.session_links import execution_link_chat, republish_chat_links
from meridian.lib.ops.runtime import (
    RuntimeAuthoritySnapshot,
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


def _display_path(env_key: str, path: Path) -> str:
    """The `$VAR` name when that variable still holds this resolved path, else the path."""

    value = path.as_posix()
    return f"${env_key}" if os.environ.get(env_key) == value else value


def render_context_lines(
    resolved: ResolvedContextPaths,
    *,
    active_work_dir: Path | None,
) -> list[str]:
    """Render context paths for CLI display; a variable is named only if it matches."""

    lines: list[str] = []
    if active_work_dir is not None:
        lines.append(f"work: {_display_path('MERIDIAN_ACTIVE_WORK_DIR', active_work_dir)}")
    else:
        lines.append("work: (no active work item — run 'meridian work start')")

    if resolved.work_archive is not None:
        lines.append(
            f"  archive: {_display_path(context_env_key('work_archive'), resolved.work_archive)}"
        )

    if resolved.kb_root is not None:
        lines.append(f"kb: {_display_path(context_env_key('kb'), resolved.kb_root)}")
    else:
        lines.append("kb: (unresolved — project state is not materialized)")

    for name in sorted(resolved.extra):
        path, _ = resolved.extra[name]
        lines.append(f"{name}: {_display_path(context_env_key(name), path)}")

    return lines


class ContextOutput(BaseModel):
    """Output for context query operation."""

    model_config = ConfigDict(frozen=True)

    work_path: str
    identity_tier: Literal["committed", "adhoc"]
    project_id: str | None
    project_identity_source: Literal["meridian.toml", "path", "unminted"]
    work_resolved: str | None
    work_source: str
    active_work_dir: str | None = None
    work_archive: str
    work_archive_resolved: str | None
    kb_path: str
    kb_resolved: str | None
    kb_source: str
    mars_root: str
    mars_scope: Literal["project", "user"]
    mars_user_root: str | None = None
    mars_fallback: Literal["active", "n/a"]
    extra_contexts: dict[str, ContextEntryOutput] = Field(default_factory=dict)
    sync_status: dict[str, str] = Field(default_factory=dict)
    render_verbose: bool = Field(default=False, exclude=True, repr=False)

    def _to_resolved_paths(self) -> ResolvedContextPaths:

        from meridian.lib.config.context_config import ContextSourceType

        extras = tuple(
            ResolvedContextEntry(
                name, ContextSourceType(entry.source), None, Path(entry.resolved), entry.path, None
            )
            for name, entry in self.extra_contexts.items()
        )
        return ResolvedContextPaths(
            work_root=Path(self.work_resolved) if self.work_resolved is not None else None,
            work_archive=(
                Path(self.work_archive_resolved) if self.work_archive_resolved is not None else None
            ),
            work_source=ContextSourceType(self.work_source),
            kb_root=Path(self.kb_resolved) if self.kb_resolved is not None else None,
            kb_source=ContextSourceType(self.kb_source),
            contexts=extras,
        )

    def format_text(self, ctx: FormatContext | None = None) -> str:
        verbose = self.render_verbose
        if ctx is not None and ctx.verbosity > 0:
            verbose = True

        lines: list[str]
        if verbose:
            lines = [
                f"identity_tier: {self.identity_tier}",
                f"project_id: {self.project_id or '(unminted)'}",
                f"project_identity_source: {self.project_identity_source}",
            ]
            lines.append("work:")
            lines.append(f"  source: {self.work_source}")
            lines.append(f"  path: {self.work_path}")
            lines.append(f"  resolved: {self.work_resolved or '(unresolved)'}")
            lines.append(f"  active: {self.active_work_dir or '(none)'}")
            lines.append(f"  archive: {self.work_archive}")
            lines.append(f"  archive_resolved: {self.work_archive_resolved or '(unresolved)'}")
            lines.append("kb:")
            lines.append(f"  source: {self.kb_source}")
            lines.append(f"  path: {self.kb_path}")
            lines.append(f"  resolved: {self.kb_resolved or '(unresolved)'}")
            for name in sorted(self.extra_contexts):
                entry = self.extra_contexts[name]
                lines.append(f"{name}:")
                lines.append(f"  source: {entry.source}")
                lines.append(f"  path: {entry.path}")
                lines.append(f"  resolved: {entry.resolved}")
        else:
            active_work_dir = Path(self.active_work_dir) if self.active_work_dir else None
            # Package resolution appends Mars root/scope observability beside
            # these identity lines.
            lines = [
                f"identity_tier: {self.identity_tier}",
                f"project_id: {self.project_id or '(unminted)'}",
                f"project_identity_source: {self.project_identity_source}",
                *render_context_lines(
                    self._to_resolved_paths(),
                    active_work_dir=active_work_dir,
                ),
            ]

        user_root = self.mars_user_root or "(same as primary)"
        lines.append(f"mars_root: {self.mars_root}")
        lines.append(f"mars_scope: {self.mars_scope}")
        lines.append(f"mars_user_root: {user_root}")
        lines.append(f"mars_fallback: {self.mars_fallback}")

        if self.sync_status:
            lines.append("")
            lines.append("Sync:")
            for name in sorted(self.sync_status):
                status_lines = self.sync_status[name].splitlines() or [""]
                lines.append(f"  {name}: {status_lines[0]}")
                for status_line in status_lines[1:]:
                    lines.append(status_line)

        return "\n".join(lines)


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
    warning: str | None = None

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

    normalized_chat_id = (chat_id or "").strip()
    return ResolvedContext.from_environment(
        explicit_project_root=project_root,
        explicit_runtime_root=runtime_root,
        explicit_chat_id=normalized_chat_id or None,
    )


def resolve_active_work_scope(
    project_root: Path,
    runtime_root: Path,
    *,
    chat_id: str | None = None,
) -> WorkScope | None:

    resolved = _resolve_runtime_context(project_root, runtime_root, chat_id=chat_id)
    return resolved.work_scope


def _join_scope_path(scope_dir: Path, relpath: str) -> Path:
    """Join a relative path under scope_dir, rejecting escapes."""

    normalized = relpath.strip()
    if not normalized:
        raise ValueError("work path relpath must not be empty.")
    relative = Path(normalized)
    # Reject rooted and drive paths in either syntax regardless of the host OS.
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
    if not resolved.exists():
        raise ValueError(f"task_dir does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"task_dir is not a directory: {resolved}")
    require_outside_clone(resolved)
    return resolved


def _resolve_effective_task_dir_output() -> TaskDirOutput:
    authority = resolve_runtime_authority_for_read()
    runtime_ctx = RuntimeContext.from_environment()
    resolved = ResolvedContext.from_environment()
    spawn_id = str(runtime_ctx.spawn_id) if runtime_ctx.spawn_id is not None else None
    effective = resolve_effective_task_dir(
        project_root=authority.project_root,
        project_state_dir=authority.project_state_dir,
        spawn_id=spawn_id,
        # Inside a managed run `MERIDIAN_TASK_DIR` is this execution's own link, not an
        # input: its pin and its item decide.
        inherited_task_dir=None if spawn_id else inheritable_task_dir(authority.project_root, None),
        work_id=resolved.work_id,
    )
    return TaskDirOutput(
        task_dir=effective.task_dir.as_posix(),
        source=effective.source,
        spawn_id=spawn_id,
        warning=effective.warning,
    )


def task_dir_sync(input: TaskDirInput) -> TaskDirOutput:

    _ = input
    return _resolve_effective_task_dir_output()


def task_dir_set_sync(input: TaskDirSetInput) -> TaskDirMutationOutput:

    authority = resolve_runtime_authority_for_write()
    runtime_ctx = RuntimeContext.from_environment()
    spawn_id = _require_session_spawn_id(runtime_ctx)
    task_dir = _validated_task_dir_path(input.path)
    if not write_spawn_scope_task_dir(authority.project_root, spawn_id, task_dir):
        raise RefNotFound(spawn_id, "spawn")
    _refresh_session_task_links(authority, runtime_ctx)
    return TaskDirMutationOutput()


def task_dir_clear_sync(input: TaskDirClearInput) -> TaskDirMutationOutput:

    _ = input
    authority = resolve_runtime_authority_for_write()
    runtime_ctx = RuntimeContext.from_environment()
    spawn_id = _require_session_spawn_id(runtime_ctx)
    if not write_spawn_scope_task_dir(authority.project_root, spawn_id, None):
        raise RefNotFound(spawn_id, "spawn")
    _refresh_session_task_links(authority, runtime_ctx)
    return TaskDirMutationOutput()


def _refresh_session_task_links(
    authority: RuntimeAuthoritySnapshot,
    runtime_ctx: RuntimeContext,
) -> None:

    spawn_id = str(runtime_ctx.spawn_id) if runtime_ctx.spawn_id is not None else ""
    if authority.runtime_root is None or not spawn_id:
        return
    entry_chat = execution_link_chat(authority.runtime_root, spawn_id=spawn_id)
    if entry_chat:
        republish_chat_links(
            authority.project_root, authority.runtime_root, entry_chat, spawn_id=spawn_id
        )


def context_sync(input: ContextInput) -> ContextOutput:
    from meridian.lib.ops.sync_daemon import project_sync_summaries

    authority = resolve_runtime_authority_for_read()
    runtime_root = authority.runtime_root
    resolved_runtime_context = (
        _resolve_runtime_context(authority.project_root, runtime_root)
        if runtime_root is not None
        else None
    )
    context_config = load_context_config(authority.project_root) or ContextConfig()
    resolved_paths = resolve_context_paths(authority.project_root, context_config)
    extra_entries = {
        c.name: ContextEntryOutput(
            source=c.source.value, path=c.path_spec, resolved=c.path.as_posix()
        )
        for c in resolved_paths.extra_contexts
        if c.path is not None
    }

    mars_roots = authority.mars_roots
    return ContextOutput(
        identity_tier=authority.identity_tier,
        project_id=authority.project_id,
        project_identity_source=authority.project_identity_source,
        work_path=context_config.work.path,
        work_resolved=(
            resolved_paths.work_root.as_posix() if resolved_paths.work_root is not None else None
        ),
        work_source=context_config.work.source.value,
        active_work_dir=(
            resolved_runtime_context.work_dir.as_posix()
            if resolved_runtime_context is not None
            and resolved_runtime_context.work_dir is not None
            else None
        ),
        work_archive=context_config.work.archive,
        work_archive_resolved=(
            resolved_paths.work_archive.as_posix()
            if resolved_paths.work_archive is not None
            else None
        ),
        kb_path=context_config.kb.path,
        kb_resolved=(
            resolved_paths.kb_root.as_posix() if resolved_paths.kb_root is not None else None
        ),
        kb_source=context_config.kb.source.value,
        mars_root=mars_roots.primary.as_posix(),
        mars_scope=mars_roots.scope,
        mars_user_root=(mars_roots.user.as_posix() if mars_roots.user is not None else None),
        mars_fallback=mars_fallback_label(mars_roots),
        extra_contexts=extra_entries,
        sync_status=project_sync_summaries(authority.project_root),
        render_verbose=input.verbose,
    )


def context_dir_sync(name: str) -> str:
    """The directory *name* resolves to, or "" when it is unresolved; KeyError if unknown."""

    authority = resolve_runtime_authority_for_read()
    resolved = ResolvedContext.from_environment(
        explicit_project_root=authority.project_root,
        explicit_runtime_root=authority.runtime_root,
    )
    path = resolved.context_dir(name)
    return path.as_posix() if path is not None else ""


def work_current_sync(input: WorkCurrentInput) -> WorkCurrentOutput:

    _ = input
    authority = resolve_runtime_authority_for_read()
    runtime_root = authority.runtime_root
    if runtime_root is None:
        return WorkCurrentOutput(work_dir=None)
    scope = resolve_active_work_scope(authority.project_root, runtime_root)

    return WorkCurrentOutput(work_dir=scope.root.as_posix() if scope is not None else None)


def work_path_sync(input: WorkPathInput) -> WorkPathOutput:

    authority = resolve_runtime_authority_for_read()
    runtime_root = authority.runtime_root
    if runtime_root is None:
        raise ValueError("Project has no identity; no active work scope exists.")
    scope = resolve_active_work_scope(authority.project_root, runtime_root)
    if scope is None:
        raise ValueError("No active work scope is resolvable for this process.")

    target = _join_scope_path(scope.root, input.relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    return WorkPathOutput(path=target.as_posix())


def work_root_sync(input: WorkRootInput) -> WorkRootOutput:

    _ = input
    authority = resolve_runtime_authority_for_read()
    context_config = load_context_config(authority.project_root) or ContextConfig()
    resolved_paths = resolve_context_paths(authority.project_root, context_config)
    if resolved_paths.work_root is None:
        raise ValueError("Project has no identity; work root is unresolved.")
    return WorkRootOutput(work_root=resolved_paths.work_root.as_posix())


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
    "context_sync",
    "resolve_active_work_scope",
    "task_dir_clear_sync",
    "task_dir_set_sync",
    "task_dir_sync",
    "work_current_sync",
    "work_path_sync",
    "work_root_sync",
]
