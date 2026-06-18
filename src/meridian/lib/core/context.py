"""Runtime context derived from MERIDIAN_* environment variables."""

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.depth import is_nested_meridian_depth
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.types import SpawnId
from meridian.lib.state.paths import resolve_work_scratch_dir, resolve_work_scratch_dir_for_project


class RuntimeContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: SpawnId | None = None
    depth: int = 0
    project_root: Path | None = None
    runtime_root: Path | None = None
    chat_id: str = ""
    work_id: str | None = None
    work_dir: Path | None = None

    @property
    def is_nested(self) -> bool:
        """Whether this context is below the primary Meridian root."""
        return is_nested_meridian_depth(self.depth)

    @classmethod
    def from_environment(cls) -> Self:
        """Build context from MERIDIAN_* environment variables."""
        resolved = ResolvedContext.from_environment()

        return cls(
            spawn_id=resolved.spawn_id,
            depth=resolved.depth,
            project_root=resolved.project_root,
            runtime_root=resolved.runtime_root,
            chat_id=resolved.chat_id,
            work_id=resolved.work_id,
            work_dir=resolved.work_dir,
        )

    def to_env_overrides(self) -> dict[str, str]:
        """Produce MERIDIAN_* env overrides for child processes."""

        overrides: dict[str, str] = {"MERIDIAN_DEPTH": str(self.depth)}
        if self.spawn_id is not None:
            overrides["MERIDIAN_SPAWN_ID"] = str(self.spawn_id)
        if self.project_root is not None:
            overrides["MERIDIAN_PROJECT_DIR"] = self.project_root.as_posix()
        if self.chat_id:
            overrides["MERIDIAN_CHAT_ID"] = self.chat_id
        if self.work_id:
            overrides["MERIDIAN_ACTIVE_WORK_ID"] = self.work_id
        if self.work_dir is not None:
            overrides["MERIDIAN_ACTIVE_WORK_DIR"] = self.work_dir.as_posix()
        elif self.work_id:
            if self.project_root is not None:
                overrides["MERIDIAN_ACTIVE_WORK_DIR"] = resolve_work_scratch_dir_for_project(
                    self.project_root,
                    self.work_id,
                ).as_posix()
            elif self.runtime_root is not None:
                overrides["MERIDIAN_ACTIVE_WORK_DIR"] = resolve_work_scratch_dir(
                    self.runtime_root,
                    self.work_id,
                ).as_posix()
        return overrides


def resolve_runtime_context(*, project_root: Path, runtime_root: Path) -> ResolvedContext:
    """Resolve runtime context with explicit roots and no environment mutation."""

    return ResolvedContext.from_environment(
        explicit_project_root=project_root,
        explicit_runtime_root=runtime_root,
    )
