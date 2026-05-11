"""Project-root Meridian path helpers."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT_IGNORE_TARGETS: tuple[str, ...] = ("meridian.local.toml",)


class ProjectConfigPaths(BaseModel):
    """Resolved project-level paths and project-root Meridian file policy."""

    model_config = ConfigDict(frozen=True)

    project_root: Path
    execution_cwd: Path
    meridian_toml: Path = Field(default=Path("."))
    meridian_local_toml: Path = Field(default=Path("."))
    workspace_ignore_targets: tuple[str, ...] = PROJECT_ROOT_IGNORE_TARGETS

    def model_post_init(self, __context: object) -> None:
        if self.meridian_toml == Path("."):
            object.__setattr__(self, "meridian_toml", self.project_root / "meridian.toml")
        if self.meridian_local_toml == Path("."):
            object.__setattr__(
                self,
                "meridian_local_toml",
                self.project_root / "meridian.local.toml",
            )


def resolve_project_config_paths(
    project_root: Path, execution_cwd: Path | None = None
) -> ProjectConfigPaths:
    """Build project paths from repository root and optional execution cwd."""

    resolved_project_root = project_root.resolve()
    resolved_execution_cwd = (execution_cwd or project_root).resolve()
    return ProjectConfigPaths(
        project_root=resolved_project_root,
        execution_cwd=resolved_execution_cwd,
        meridian_toml=resolved_project_root / "meridian.toml",
        meridian_local_toml=resolved_project_root / "meridian.local.toml",
    )
