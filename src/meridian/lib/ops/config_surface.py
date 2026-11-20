from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.config.dynamic_sections import DynamicConfigSections
from meridian.lib.config.loader import ConfigValueSource, load_config_with_sources
from meridian.lib.config.models import MeridianConfig
from meridian.lib.config.project_config_state import (
    ProjectConfigState,
    resolve_project_config_state,
)
from meridian.lib.config.project_root import resolve_user_config_path
from meridian.lib.config.workspace import (
    WorkspaceFinding,
    WorkspaceSnapshot,
    WorkspaceStatus,
    get_projectable_roots,
    resolve_workspace_snapshot,
)
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import HarnessContract, WorkspaceRoots
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.workspace_projection import (
    WorkspaceApplicability,
    project_workspace_roots,
)
from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot, resolve_runtime_authority_for_read


class ConfigSurfaceWorkspaceRoots(BaseModel):
    """Workspace root counts surfaced by inspection commands."""

    model_config = ConfigDict(frozen=True)

    count: int
    projected: int
    skipped: int


class ConfigSurfaceWorkspaceRootDetail(BaseModel):
    """Per-root workspace detail surfaced by verbose text/JSON inspection."""

    model_config = ConfigDict(frozen=True)

    name: str
    source: str
    declared_path: str
    resolved_path: str
    status: str


def _contracts_in_id_order() -> tuple[tuple[HarnessId, HarnessContract], ...]:
    contracts = get_default_harness_registry().contracts()
    return tuple(
        (harness_id, contracts[harness_id]) for harness_id in HarnessId if harness_id in contracts
    )


class ConfigSurfaceWorkspace(BaseModel):
    """Minimal workspace summary payload used by `config show`."""

    model_config = ConfigDict(frozen=True)

    status: WorkspaceStatus
    sources: tuple[str, ...] = ()
    roots: ConfigSurfaceWorkspaceRoots
    applicability: dict[str, WorkspaceApplicability]
    roots_detail: tuple[ConfigSurfaceWorkspaceRootDetail, ...] = ()

    @classmethod
    def from_snapshot(cls, snapshot: WorkspaceSnapshot) -> ConfigSurfaceWorkspace:
        projectable_roots = get_projectable_roots(snapshot)
        return cls(
            status=snapshot.status,
            sources=tuple(path.as_posix() for path in snapshot.source_paths),
            roots=ConfigSurfaceWorkspaceRoots(
                count=snapshot.roots_count,
                projected=len(projectable_roots),
                skipped=snapshot.roots_count - len(projectable_roots),
            ),
            applicability={
                harness_id.value: project_workspace_roots(
                    support=contract.workspace_roots,
                    roots=projectable_roots,
                ).applicability
                for harness_id, contract in _contracts_in_id_order()
                if contract.workspace_roots is not WorkspaceRoots.NONE
            },
            roots_detail=tuple(
                ConfigSurfaceWorkspaceRootDetail(
                    name=root.name,
                    source=root.source,
                    declared_path=root.declared_path,
                    resolved_path=root.resolved_path.as_posix(),
                    status="projected" if root.enabled and root.exists else "skipped",
                )
                for root in snapshot.roots
            ),
        )


class ConfigSurface(BaseModel):
    """Observed config surface shared by inspection commands."""

    model_config = ConfigDict(frozen=True)

    authority: RuntimeAuthoritySnapshot
    project_root: Path
    project_config: ProjectConfigState
    user_config_path: Path | None
    resolved_config: MeridianConfig
    resolved_dynamic_config: DynamicConfigSections
    config_sources: tuple[ConfigValueSource, ...]
    workspace: ConfigSurfaceWorkspace
    workspace_findings: tuple[WorkspaceFinding, ...] = ()
    warning: str | None = None


def build_config_surface(project_root: Path | RuntimeAuthoritySnapshot) -> ConfigSurface:

    authority = (
        project_root
        if isinstance(project_root, RuntimeAuthoritySnapshot)
        else resolve_runtime_authority_for_read(project_root)
    )
    resolved_root = authority.project_root
    project_config_paths = authority.project_config_paths
    user_config_path = resolve_user_config_path(None)
    warning: str | None = None
    if not resolved_root.exists():
        warning = f"Resolved project root '{resolved_root.as_posix()}' does not exist on disk."
    workspace_snapshot = resolve_workspace_snapshot(
        resolved_root,
        config_paths=project_config_paths,
    )

    loaded = load_config_with_sources(
        resolved_root,
        authority=authority,
        user_config=user_config_path,
    )
    return ConfigSurface(
        authority=authority,
        project_root=resolved_root,
        project_config=resolve_project_config_state(
            resolved_root,
            project_paths=project_config_paths,
        ),
        user_config_path=user_config_path,
        resolved_config=loaded.config,
        resolved_dynamic_config=loaded.dynamic,
        config_sources=loaded.sources,
        workspace=ConfigSurfaceWorkspace.from_snapshot(workspace_snapshot),
        workspace_findings=workspace_snapshot.findings,
        warning=warning,
    )
