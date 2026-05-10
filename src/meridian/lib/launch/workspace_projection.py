"""Workspace-root projection contract shared by harness adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from meridian.lib.core.types import HarnessId

WorkspaceApplicability = Literal[
    "active",
    "unsupported:requires_config_generation",
    "ignored:no_roots",
]
WorkspaceProjectionDiagnosticCode = Literal["workspace_opencode_parent_env_suppressed"]

OPENCODE_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"


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
    env_overrides: dict[str, str] = Field(default_factory=dict)
    diagnostics: tuple[WorkspaceProjectionDiagnostic, ...] = ()


def _project_claude_workspace_args(roots: tuple[Path, ...]) -> tuple[str, ...]:
    projected: list[str] = []
    for root in roots:
        projected.extend(("--add-dir", root.as_posix()))
    return tuple(projected)


def _coerce_external_directory_entries(raw: object) -> dict[str, str]:
    if isinstance(raw, dict):
        raw_mapping = cast("dict[Any, Any]", raw)
        return {str(path): str(action) for path, action in raw_mapping.items()}
    if isinstance(raw, list):
        raw_list = cast("list[Any]", raw)
        return {str(path): "allow" for path in raw_list}
    return {}


def _merge_opencode_workspace_config(
    *,
    roots: tuple[Path, ...],
    parent_opencode_config_content: str | None,
) -> str:
    parent_raw = (parent_opencode_config_content or "").strip()
    merged: dict[str, object] = {}
    if parent_raw:
        try:
            parsed = json.loads(parent_raw)
            if isinstance(parsed, dict):
                parsed_mapping = cast("dict[Any, Any]", parsed)
                merged = {str(key): value for key, value in parsed_mapping.items()}
        except json.JSONDecodeError:
            merged = {}

    permission_raw = merged.get("permission")
    permission: dict[str, object] = (
        {
            str(key): value
            for key, value in cast("dict[Any, Any]", permission_raw).items()
        }
        if isinstance(permission_raw, dict)
        else {}
    )
    external_directory = _coerce_external_directory_entries(permission.get("external_directory"))
    for root in roots:
        external_directory[root.as_posix() + "/**"] = "allow"
    permission["external_directory"] = external_directory
    merged["permission"] = permission
    return json.dumps(merged, separators=(",", ":"), sort_keys=True)


def project_workspace_roots(
    *,
    harness_id: HarnessId,
    roots: tuple[Path, ...],
    parent_opencode_config_content: str | None = None,
) -> ProjectionResult:
    """Project workspace roots for one harness launch/inspection pass."""

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
        return ProjectionResult(
            applicability="active",
            env_overrides={
                OPENCODE_CONFIG_CONTENT_ENV: _merge_opencode_workspace_config(
                    roots=roots,
                    parent_opencode_config_content=parent_opencode_config_content,
                )
            },
        )

    return ProjectionResult(applicability="unsupported:requires_config_generation")


__all__ = [
    "OPENCODE_CONFIG_CONTENT_ENV",
    "ProjectionResult",
    "WorkspaceApplicability",
    "WorkspaceProjectionDiagnostic",
    "WorkspaceProjectionDiagnosticCode",
    "project_workspace_roots",
]
