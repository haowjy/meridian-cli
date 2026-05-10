"""Context path resolution and rendering."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from meridian.lib.config.context_config import (
    ArbitraryContextConfig,
    ContextConfig,
    ContextSourceType,
)
from meridian.lib.state.user_paths import get_project_id
from meridian.plugin_api.git import resolve_clone_path


@dataclass(frozen=True)
class ResolvedContextPaths:
    """Resolved context paths after substitution."""

    work_root: Path
    work_archive: Path
    work_source: ContextSourceType
    kb_root: Path
    kb_source: ContextSourceType
    extra: dict[str, tuple[Path, ContextSourceType]]


def context_uses_project_placeholder(config: ContextConfig) -> bool:
    """Return whether any configured context path references ``{project}``."""

    if "{project}" in config.work.path:
        return True
    if "{project}" in config.work.archive:
        return True
    if "{project}" in config.kb.path:
        return True

    extras_raw = getattr(config, "__pydantic_extra__", None)
    extras = cast("dict[str, object]", extras_raw) if isinstance(extras_raw, dict) else {}
    for value in extras.values():
        parsed = (
            value
            if isinstance(value, ArbitraryContextConfig)
            else ArbitraryContextConfig.model_validate(value)
        )
        if "{project}" in parsed.path:
            return True
    return False


def _resolve_path(
    path_spec: str,
    project_root: Path,
    project_id: str | None,
    *,
    source: ContextSourceType = ContextSourceType.LOCAL,
    remote: str | None = None,
) -> Path:
    """Resolve one path spec with substitution and root rules.

    When source is GIT and remote is provided, resolves path relative to
    the auto-cloned repository location. The clone itself is handled lazily
    by git-autosync hooks, not here.
    """
    if project_id and "{project}" in path_spec:
        path_spec = path_spec.replace("{project}", project_id)

    # For git-backed contexts, resolve relative to the expected clone location
    if source == ContextSourceType.GIT and isinstance(remote, str) and remote.strip():
        clone_root = resolve_clone_path(remote.strip())
        return clone_root / path_spec

    candidate = Path(path_spec).expanduser()
    if candidate.is_absolute():
        return candidate
    return project_root / candidate


def resolve_context_paths(
    project_root: Path,
    config: ContextConfig,
    project_id: str | None = None,
) -> ResolvedContextPaths:
    """Resolve context paths from config."""

    if project_id is None:
        project_id = get_project_id(project_root / ".meridian")

    work_root = _resolve_path(
        config.work.path,
        project_root,
        project_id,
        source=config.work.source,
        remote=config.work.remote,
    )
    work_archive = _resolve_path(
        config.work.archive,
        project_root,
        project_id,
        source=config.work.source,
        remote=config.work.remote,
    )
    kb_root = _resolve_path(
        config.kb.path,
        project_root,
        project_id,
        source=config.kb.source,
        remote=config.kb.remote,
    )

    extra: dict[str, tuple[Path, ContextSourceType]] = {}
    extras_raw = getattr(config, "__pydantic_extra__", None)
    extras = cast("dict[str, object]", extras_raw) if isinstance(extras_raw, dict) else {}
    for name, value in extras.items():
        parsed = (
            value
            if isinstance(value, ArbitraryContextConfig)
            else ArbitraryContextConfig.model_validate(value)
        )
        extra[name] = (
            _resolve_path(
                parsed.path,
                project_root,
                project_id,
                source=parsed.source,
                remote=parsed.remote,
            ),
            parsed.source,
        )

    return ResolvedContextPaths(
        work_root=work_root,
        work_archive=work_archive,
        work_source=config.work.source,
        kb_root=kb_root,
        kb_source=config.kb.source,
        extra=extra,
    )


def context_env_key(name: str) -> str:
    """Derive the ``MERIDIAN_CONTEXT_{NAME}_DIR`` env var key for a named context."""

    env_name = "".join(c if c.isalnum() else "_" for c in name.upper()).strip("_")
    return f"MERIDIAN_CONTEXT_{env_name}_DIR"


def render_context_lines(
    resolved: ResolvedContextPaths,
    *,
    check_env: bool = True,
    active_work_dir: Path | None = None,
) -> list[str]:
    """Render context paths as display lines.

    When *check_env* is True (default — CLI display), each line shows
    ``$ENV_VAR`` when the env var is set and matches the resolved path,
    otherwise the resolved path.  When False (prompt injection), each line
    always shows ``$ENV_VAR (resolved_path)`` regardless of current env
    state, since the env vars will be set by the time the agent reads it.
    """

    lines: list[str] = []

    active_work_resolved = (
        active_work_dir.as_posix()
        if active_work_dir is not None
        else os.getenv("MERIDIAN_ACTIVE_WORK_DIR", "").strip()
    )
    if active_work_resolved:
        if check_env:
            if os.getenv("MERIDIAN_ACTIVE_WORK_DIR", "") == active_work_resolved:
                lines.append("work: $MERIDIAN_ACTIVE_WORK_DIR")
            else:
                lines.append(f"work: {active_work_resolved}")
        else:
            lines.append(f"work: $MERIDIAN_ACTIVE_WORK_DIR ({active_work_resolved})")
    else:
        lines.append("work: (no active work item — run 'meridian work start')")

    archive_resolved = resolved.work_archive.as_posix()
    if archive_resolved:
        archive_env_key = context_env_key("work_archive")
        if check_env:
            if os.getenv(archive_env_key, "") == archive_resolved:
                lines.append(f"  archive: ${archive_env_key}")
            else:
                lines.append(f"  archive: {archive_resolved}")
        else:
            lines.append(f"  archive: ${archive_env_key} ({archive_resolved})")

    kb_resolved = resolved.kb_root.as_posix()
    kb_env_key = context_env_key("kb")
    if check_env:
        if os.getenv(kb_env_key, "") == kb_resolved:
            lines.append(f"kb: ${kb_env_key}")
        else:
            lines.append(f"kb: {kb_resolved}")
    else:
        lines.append(f"kb: ${kb_env_key} ({kb_resolved})")

    for name in sorted(resolved.extra):
        path, _ = resolved.extra[name]
        env_key = context_env_key(name)
        path_str = path.as_posix()
        if check_env:
            if os.getenv(env_key, "") == path_str:
                lines.append(f"{name}: ${env_key}")
            else:
                lines.append(f"{name}: {path_str}")
        else:
            lines.append(f"{name}: ${env_key} ({path_str})")

    return lines
