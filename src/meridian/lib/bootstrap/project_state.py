"""Project-local bootstrap services for Meridian state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.config.context_config import ContextConfig, ContextSourceType
from meridian.lib.state.paths import (
    ProjectPaths,
    ensure_gitignore,
    resolve_project_paths,
    resolve_project_paths_for_write,
)
from meridian.lib.state.paths import (
    load_context_config as _load_context_config,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectLayoutSnapshot:
    """Read-only snapshot of project-local Meridian layout."""

    project_root: Path
    paths: ProjectPaths
    context_config: ContextConfig | None

    @property
    def project_state_dir(self) -> Path:
        return self.paths.root_dir

    @property
    def id_file(self) -> Path:
        return self.paths.id_file

    @property
    def kb_dir(self) -> Path:
        return self.paths.kb_dir

    @property
    def work_dir(self) -> Path:
        return self.paths.work_dir

    @property
    def work_archive_dir(self) -> Path:
        return self.paths.work_archive_dir


def load_context_config(project_root: Path) -> ContextConfig | None:
    """Load merged context config for a project without mutating state."""

    return _load_context_config(project_root)


def resolve_layout(project_root: Path) -> ProjectLayoutSnapshot:
    """Resolve project-local layout without creating files or directories."""

    context_config = load_context_config(project_root)
    return ProjectLayoutSnapshot(
        project_root=project_root,
        paths=resolve_project_paths(project_root),
        context_config=context_config,
    )


def _has_non_empty_remote(remote: str | None) -> bool:
    return isinstance(remote, str) and bool(remote.strip())


def ensure_project_dirs(project_root: Path) -> None:
    """Create project-local directories required by the context policy."""

    context_config = load_context_config(project_root)
    project_paths = resolve_project_paths_for_write(project_root)
    project_paths.root_dir.mkdir(parents=True, exist_ok=True)

    if context_config is None:
        project_paths.kb_dir.mkdir(parents=True, exist_ok=True)
        project_paths.work_dir.mkdir(parents=True, exist_ok=True)
        project_paths.work_archive_dir.mkdir(parents=True, exist_ok=True)
        return

    kb_git_with_remote = (
        context_config.kb.source == ContextSourceType.GIT
        and _has_non_empty_remote(context_config.kb.remote)
    )
    work_git_with_remote = (
        context_config.work.source == ContextSourceType.GIT
        and _has_non_empty_remote(context_config.work.remote)
    )

    if context_config.kb.source == ContextSourceType.GIT and not kb_git_with_remote:
        logger.warning(
            "context_source_git_missing_remote_fallback_local",
            extra={"context": "kb", "configured_remote": context_config.kb.remote},
        )
    if context_config.work.source == ContextSourceType.GIT and not work_git_with_remote:
        logger.warning(
            "context_source_git_missing_remote_fallback_local",
            extra={"context": "work", "configured_remote": context_config.work.remote},
        )

    if not kb_git_with_remote:
        project_paths.kb_dir.mkdir(parents=True, exist_ok=True)
    if not work_git_with_remote:
        project_paths.work_dir.mkdir(parents=True, exist_ok=True)
        project_paths.work_archive_dir.mkdir(parents=True, exist_ok=True)


def ensure_project_gitignore(project_root: Path) -> None:
    """Reconcile the project-local `.meridian/.gitignore` for write paths."""

    ensure_gitignore(project_root)
