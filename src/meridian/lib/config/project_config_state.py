"""Project-config state shared by loader, config ops, and bootstrap paths."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.project_paths import ProjectConfigPaths, resolve_project_config_paths

ProjectConfigStatus = Literal["absent", "present"]


class ProjectConfigState(BaseModel):
    """Observed project-config state for `<project-root>/meridian.toml`."""

    model_config = ConfigDict(frozen=True)

    status: ProjectConfigStatus
    write_path: Path
    path: Path | None = None

    @property
    def is_present(self) -> bool:
        """Return True when project config exists on disk."""

        return self.status == "present"

    @classmethod
    def from_project_paths(cls, project_paths: ProjectConfigPaths) -> "ProjectConfigState":
        """Build config state from resolved project paths."""

        write_path = project_paths.meridian_toml
        if write_path.is_file():
            return cls(status="present", write_path=write_path, path=write_path)
        return cls(status="absent", write_path=write_path, path=None)


def resolve_project_config_state(
    project_root: Path,
    *,
    project_paths: ProjectConfigPaths | None = None,
) -> ProjectConfigState:
    """Resolve canonical project-config state for one repo root."""

    return ProjectConfigState.from_project_paths(
        project_paths or resolve_project_config_paths(project_root)
    )
