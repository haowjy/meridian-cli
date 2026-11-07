from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.depth import is_nested_meridian_depth
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.core.types import SpawnId


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
        return is_nested_meridian_depth(self.depth)

    @classmethod
    def from_environment(
        cls,
        *,
        project_root: Path | None = None,
        runtime_root: Path | None = None,
    ) -> Self:
        resolved = ResolvedContext.from_environment(
            explicit_project_root=project_root,
            explicit_runtime_root=runtime_root,
        )

        return cls(
            spawn_id=resolved.spawn_id,
            depth=resolved.depth,
            project_root=resolved.project_root,
            runtime_root=resolved.runtime_root,
            chat_id=resolved.chat_id,
            work_id=resolved.work_id,
            work_dir=resolved.work_dir,
        )


def resolve_runtime_context(*, project_root: Path, runtime_root: Path | None) -> ResolvedContext:

    return ResolvedContext.from_environment(
        explicit_project_root=project_root,
        explicit_runtime_root=runtime_root,
    )
