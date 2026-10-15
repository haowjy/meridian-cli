"""Runtime context derived from MERIDIAN_* environment variables."""

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.depth import is_nested_meridian_depth
from meridian.lib.core.identity import identity_env
from meridian.lib.core.refs import spawn_of
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.types import RunId, SpawnId
from meridian.lib.state.paths import resolve_work_scratch_dir_for_project


class RuntimeContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    spawn_id: SpawnId | None = None
    run_id: RunId | None = None
    depth: int = 0
    project_root: Path | None = None
    runtime_root: Path | None = None
    chat_id: str = ""
    work_id: str | None = None
    work_dir: Path | None = None

    @property
    def run_key(self) -> RunId | None:
        """Exact row for explicit contexts as well as environment-derived ones."""
        if self.run_id is None:
            if self.spawn_id is not None:
                raise ValueError("RuntimeContext with spawn_id requires an exact run_id")
            return None
        if self.spawn_id is not None and spawn_of(self.run_id) != self.spawn_id:
            raise ValueError("RuntimeContext spawn_id and run_id do not match")
        return self.run_id

    @property
    def is_nested(self) -> bool:
        """Whether this context is below the primary Meridian root."""
        return is_nested_meridian_depth(self.depth)

    @classmethod
    def from_environment(
        cls,
        *,
        project_root: Path | None = None,
        runtime_root: Path | None = None,
    ) -> Self:
        """Build context from MERIDIAN_* environment variables."""
        resolved = ResolvedContext.from_environment(
            explicit_project_root=project_root,
            explicit_runtime_root=runtime_root,
        )

        return cls(
            spawn_id=resolved.spawn_id,
            run_id=resolved.run_id,
            depth=resolved.depth,
            project_root=resolved.project_root,
            runtime_root=resolved.runtime_root,
            chat_id=resolved.chat_id,
            work_id=resolved.work_id,
            work_dir=resolved.work_dir,
        )

    def to_env_overrides(self) -> dict[str, str]:
        """Produce MERIDIAN_* env overrides for child processes."""

        overrides: dict[str, str] = {"_MERIDIAN_DEPTH": str(self.depth)}
        if self.spawn_id is not None:
            run_id = self.run_key
            assert run_id is not None
            overrides.update(identity_env(self.spawn_id, run_id))
        if self.project_root is not None:
            overrides["MERIDIAN_PROJECT_DIR"] = self.project_root.as_posix()
        if self.chat_id:
            overrides["MERIDIAN_CHAT_ID"] = self.chat_id
        if self.work_id:
            overrides["MERIDIAN_ACTIVE_WORK_ID"] = self.work_id
        if self.work_dir is not None:
            overrides["MERIDIAN_ACTIVE_WORK_DIR"] = self.work_dir.as_posix()
        elif self.work_id and self.project_root is not None:
            overrides["MERIDIAN_ACTIVE_WORK_DIR"] = resolve_work_scratch_dir_for_project(
                self.project_root,
                self.work_id,
            ).as_posix()
        return overrides


def resolve_runtime_context(*, project_root: Path, runtime_root: Path | None) -> ResolvedContext:
    """Resolve runtime context with explicit roots and no environment mutation."""

    return ResolvedContext.from_environment(
        explicit_project_root=project_root,
        explicit_runtime_root=runtime_root,
    )
