from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meridian.lib.config.context_config import (
    ContextConfig,
    ContextSourceType,
    extra_contexts,
)
from meridian.lib.state.project_identity import (
    AdHoc,
    ProjectIdentity,
    resolve_project_identity,
    state_id,
)
from meridian.plugin_api.git import resolve_clone_path


@dataclass(frozen=True)
class ResolvedContext:
    """One configured context with its resolved path, source and remote."""

    name: str
    source: ContextSourceType
    remote: str | None
    path: Path | None
    # The `autosync` key as written; `None` is the default for the clone.
    autosync: bool | None = None


@dataclass(frozen=True)
class ResolvedContextPaths:
    """Resolved context paths after substitution."""

    work_root: Path | None
    work_archive: Path | None
    work_source: ContextSourceType
    kb_root: Path | None
    kb_source: ContextSourceType
    extra: dict[str, tuple[Path, ContextSourceType]]
    # `work`, `kb`, then each named context whose path resolved, in config order.
    contexts: tuple[ResolvedContext, ...] = ()


def context_uses_project_placeholder(config: ContextConfig) -> bool:
    """Return whether any configured context path references ``{project}``."""

    if "{project}" in config.work.path:
        return True
    if "{project}" in config.work.archive:
        return True
    if "{project}" in config.kb.path:
        return True

    return any("{project}" in parsed.path for parsed in extra_contexts(config).values())


def _resolve_path(
    path_spec: str,
    project_root: Path,
    project_id: str | None,
    *,
    source: ContextSourceType = ContextSourceType.LOCAL,
    remote: str | None = None,
    allow_project_relative: bool = True,
) -> Path | None:
    """Resolve one path spec with substitution and root rules.

    Returns None when a ``{project}`` placeholder is present but no project ID
    is available (fresh project on a read path).

    When source is GIT and remote is provided, resolves path relative to
    the auto-cloned repository location. The clone itself is made by the
    sync daemon, not here.
    """
    if "{project}" in path_spec:
        if project_id:
            path_spec = path_spec.replace("{project}", project_id)
        else:
            # Can't resolve — fresh project on read path
            return None

    if "{user_home}" in path_spec:
        from meridian.lib.state.user_paths import get_user_home

        path_spec = path_spec.replace("{user_home}", str(get_user_home()))

    # For git-backed contexts, resolve relative to the expected clone location
    if source == ContextSourceType.GIT and isinstance(remote, str) and remote.strip():
        clone_root = resolve_clone_path(remote.strip())
        return clone_root / path_spec

    candidate = Path(path_spec).expanduser()
    if candidate.is_absolute():
        return candidate
    if not allow_project_relative:
        return None
    return project_root / candidate


def resolve_context_paths(
    project_root: Path,
    config: ContextConfig,
    project_id: str | None = None,
    *,
    identity: ProjectIdentity | None = None,
) -> ResolvedContextPaths:
    """Resolve context paths from config.

    Paths containing ``{project}`` remain unresolved without a project ID.
    """

    resolved_identity = identity or resolve_project_identity(project_root)
    if project_id is None:
        project_id = state_id(resolved_identity)
    allow_project_relative = not isinstance(resolved_identity, AdHoc)

    work_root_resolved = _resolve_path(
        config.work.path,
        project_root,
        project_id,
        source=config.work.source,
        remote=config.work.remote,
        allow_project_relative=allow_project_relative,
    )
    work_archive_resolved = _resolve_path(
        config.work.archive,
        project_root,
        project_id,
        source=config.work.source,
        remote=config.work.remote,
        allow_project_relative=allow_project_relative,
    )
    kb_root_resolved = _resolve_path(
        config.kb.path,
        project_root,
        project_id,
        source=config.kb.source,
        remote=config.kb.remote,
        allow_project_relative=allow_project_relative,
    )

    extra: dict[str, tuple[Path, ContextSourceType]] = {}
    named = [
        ResolvedContext(
            "work",
            config.work.source,
            config.work.remote,
            work_root_resolved,
            config.work.autosync,
        ),
        ResolvedContext(
            "kb", config.kb.source, config.kb.remote, kb_root_resolved, config.kb.autosync
        ),
    ]
    for name, parsed in extra_contexts(config).items():
        resolved = _resolve_path(
            parsed.path,
            project_root,
            project_id,
            source=parsed.source,
            remote=parsed.remote,
            allow_project_relative=allow_project_relative,
        )
        if resolved is None:
            # Skip extra contexts that can't resolve without a project ID
            continue
        extra[name] = (resolved, parsed.source)
        named.append(
            ResolvedContext(
                name.strip().lower(), parsed.source, parsed.remote, resolved, parsed.autosync
            )
        )

    return ResolvedContextPaths(
        work_root=work_root_resolved,
        work_archive=work_archive_resolved,
        work_source=config.work.source,
        kb_root=kb_root_resolved,
        kb_source=config.kb.source,
        extra=extra,
        contexts=tuple(named),
    )


def context_env_key(name: str) -> str:
    """Derive the ``MERIDIAN_CONTEXT_{NAME}_DIR`` env var key for a named context."""

    env_name = "".join(c if c.isalnum() else "_" for c in name.upper()).strip("_")
    return f"MERIDIAN_CONTEXT_{env_name}_DIR"
