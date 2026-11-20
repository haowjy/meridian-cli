from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.types import HarnessId

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


# Harnesses with a workspace projection; `config show` reports only these. Any other
# harness projects as unsupported, so a new harness that supports roots is added here.
WORKSPACE_PROJECTING_HARNESSES: tuple[HarnessId, ...] = (
    HarnessId.CLAUDE,
    HarnessId.CODEX,
    HarnessId.OPENCODE,
)


def _project_claude_workspace_args(roots: tuple[Path, ...]) -> tuple[str, ...]:
    projected: list[str] = []
    for root in roots:
        projected.extend(("--add-dir", root.as_posix()))
    return tuple(projected)


def project_workspace_roots(
    *,
    harness_id: HarnessId,
    roots: tuple[Path, ...],
) -> ProjectionResult:

    if not roots:
        return ProjectionResult(applicability="ignored:no_roots")

    # Claude and Codex both support --add-dir. Note: Codex --add-dir only grants
    # write access; in read-only sandbox modes the flag is ignored. We project
    # it anyway for write scenarios and document this as semi-supported.
    if harness_id in (HarnessId.CLAUDE, HarnessId.CODEX):
        return ProjectionResult(
            applicability="active",
            args=_project_claude_workspace_args(roots),
        )

    if harness_id == HarnessId.OPENCODE:
        # Roots become ``external_directory`` grants in ``OpenCodeAdapter.project_launch_env``.
        return ProjectionResult(applicability="active")

    return ProjectionResult(applicability="unsupported:requires_config_generation")


__all__ = [
    "WORKSPACE_PROJECTING_HARNESSES",
    "ProjectionResult",
    "WorkspaceApplicability",
    "WorkspaceProjectionDiagnostic",
    "WorkspaceProjectionDiagnosticCode",
    "project_workspace_roots",
]
