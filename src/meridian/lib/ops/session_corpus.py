"""Session-search corpus resolution helpers."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from meridian.lib.config.workspace import get_projectable_roots, resolve_workspace_snapshot
from meridian.lib.ops.runtime import resolve_runtime_authority_for_read
from meridian.lib.ops.work_sessions import work_session_chat_ids
from meridian.lib.state.user_paths import get_user_home


class SessionCorpusScope(NamedTuple):
    project_root: Path | None
    runtime_root: Path
    label: str
    chat_filter: frozenset[str] | None = None


def _runtime_has_session_data(runtime_root: Path) -> bool:
    return runtime_root.is_dir() and (
        (runtime_root / "sessions.jsonl").is_file()
        or any((runtime_root / "spawns").glob("p*"))
    )


def _workspace_scopes(current_project_root: Path) -> tuple[SessionCorpusScope, ...]:
    snapshot = resolve_workspace_snapshot(current_project_root)
    scopes: list[SessionCorpusScope] = []
    for root in get_projectable_roots(snapshot):
        authority = resolve_runtime_authority_for_read(root)
        runtime_root = authority.runtime_root
        if runtime_root is None:
            continue
        if not _runtime_has_session_data(runtime_root):
            continue
        scopes.append(
            SessionCorpusScope(
                project_root=authority.project_root,
                runtime_root=runtime_root,
                label=authority.project_root.as_posix(),
            )
        )
    return tuple(scopes)


def _global_runtime_scopes() -> tuple[SessionCorpusScope, ...]:
    projects_root = get_user_home() / "projects"
    if not projects_root.is_dir():
        return ()

    scopes: list[SessionCorpusScope] = []
    for runtime_root in sorted(projects_root.iterdir()):
        if not runtime_root.is_dir():
            continue
        if not _runtime_has_session_data(runtime_root):
            continue
        scopes.append(
            SessionCorpusScope(
                project_root=None,
                runtime_root=runtime_root,
                label=f"runtime:{runtime_root.name}",
            )
        )
    return tuple(scopes)


def resolve_session_search_corpus(
    *,
    project_root: Path,
    runtime_root: Path | None,
    workspace: bool,
    global_scope: bool,
    work_id: str | None,
) -> tuple[SessionCorpusScope, ...]:
    """Resolve ordered search scope roots for session search."""

    normalized_work_id = (work_id or "").strip()
    if sum((workspace, global_scope, bool(normalized_work_id))) > 1:
        raise ValueError("Use only one scope flag: --workspace, --global, or --work.")

    current_scope = (
        SessionCorpusScope(
            project_root=project_root,
            runtime_root=runtime_root,
            label=project_root.as_posix(),
        )
        if runtime_root is not None
        else None
    )
    if normalized_work_id:
        if current_scope is None:
            return ()
        chat_filter = frozenset(
            work_session_chat_ids(
                project_root,
                current_scope.runtime_root,
                normalized_work_id,
                include_all=True,
            )
        )
        return (current_scope._replace(chat_filter=chat_filter),)

    scopes: list[SessionCorpusScope] = [current_scope] if current_scope is not None else []
    if workspace:
        scopes.extend(_workspace_scopes(project_root))
    if global_scope:
        scopes.extend(_global_runtime_scopes())

    deduped: list[SessionCorpusScope] = []
    seen_runtime_roots: set[Path] = set()
    for scope in scopes:
        key = scope.runtime_root.resolve()
        if key in seen_runtime_roots:
            continue
        seen_runtime_roots.add(key)
        deduped.append(scope)
    return tuple(deduped)


__all__ = [
    "SessionCorpusScope",
    "resolve_session_search_corpus",
]
