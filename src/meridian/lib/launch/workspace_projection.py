from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.harness.adapter import WorkspaceRoots

WorkspaceApplicability = Literal[
    "active",
    "unsupported:requires_config_generation",
    "ignored:no_roots",
]
WorkspaceProjectionDiagnosticCode = Literal["workspace_opencode_parent_env_suppressed"]


class WorkspaceProjectionDiagnostic(BaseModel):
    """One user-visible diagnostic emitted by workspace projection."""

    model_config = ConfigDict(frozen=True)

    code: WorkspaceProjectionDiagnosticCode
    message: str
    payload: dict[str, object] | None = None


class ProjectionResult(BaseModel):
    """Projection result consumed by launch composition and inspection surfaces."""

    model_config = ConfigDict(frozen=True)

    applicability: WorkspaceApplicability
    args: tuple[str, ...] = ()
    diagnostics: tuple[WorkspaceProjectionDiagnostic, ...] = ()


def _project_add_dir_args(roots: tuple[Path, ...]) -> tuple[str, ...]:
    projected: list[str] = []
    for root in roots:
        projected.extend(("--add-dir", root.as_posix()))
    return tuple(projected)


def project_workspace_roots(
    *,
    support: WorkspaceRoots,
    roots: tuple[Path, ...],
) -> ProjectionResult:

    if not roots:
        return ProjectionResult(applicability="ignored:no_roots")

    # Codex --add-dir only grants write access; in read-only sandbox modes the flag is
    # ignored. We project it anyway for write scenarios and document this as semi-supported.
    if support is WorkspaceRoots.ADD_DIR_ARGS:
        return ProjectionResult(
            applicability="active",
            args=_project_add_dir_args(roots),
        )

    if support is WorkspaceRoots.ENV:
        # The adapter's ``project_launch_env`` turns the roots into its own grants.
        return ProjectionResult(applicability="active")

    return ProjectionResult(applicability="unsupported:requires_config_generation")


__all__ = [
    "ProjectionResult",
    "WorkspaceApplicability",
    "WorkspaceProjectionDiagnostic",
    "WorkspaceProjectionDiagnosticCode",
    "project_workspace_roots",
]
