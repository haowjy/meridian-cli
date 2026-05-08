"""Bootstrap facade combining project, runtime, and config services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.bootstrap import config as bootstrap_config
from meridian.lib.bootstrap import project_state, runtime_state
from meridian.lib.bootstrap.project_state import ProjectLayoutSnapshot
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.ops.runtime import (
    RuntimeAuthoritySnapshot,
    resolve_project_authority,
    resolve_runtime_authority_for_read,
    resolve_runtime_authority_for_write,
)
from meridian.lib.service_context import (
    ApplicationContext,
    ApplicationServices,
    ChatEntryPoint,
    ExtensionEntryPoint,
    SpawnEntryPoint,
)

if TYPE_CHECKING:
    from meridian.lib.core.lifecycle import SpawnLifecycleService
    from meridian.lib.core.spawn_service import SpawnApplicationService


@dataclass(frozen=True)
class ProjectReadContext:
    authority: RuntimeAuthoritySnapshot
    project_root: Path
    layout: ProjectLayoutSnapshot
    config: MeridianConfig | None


@dataclass(frozen=True)
class RuntimeReadContext(ProjectReadContext):
    runtime_root: Path | None


@dataclass(frozen=True)
class ProjectWriteContext(ProjectReadContext):
    migration_ran: bool = True
    project_dirs_ensured: bool = True


@dataclass(frozen=True)
class RuntimeWriteContext(RuntimeReadContext):
    migration_ran: bool = True
    project_dirs_ensured: bool = True
    runtime_dirs_ensured: bool = True


def _application_context_from_runtime(
    prepared: RuntimeReadContext | RuntimeWriteContext,
) -> ApplicationContext:
    """Project bootstrap state onto the shared application context carrier."""

    authority = getattr(prepared, "authority", None)
    return ApplicationContext(
        project_root=prepared.project_root,
        runtime_root=prepared.runtime_root,
        config=prepared.config,
        authority=authority,
    )


def prepare_for_project_read(project_root: Path) -> ProjectReadContext:
    """Prepare read-only project context without filesystem mutation."""

    authority = resolve_project_authority(project_root)
    return ProjectReadContext(
        authority=authority,
        project_root=authority.project_root,
        layout=project_state.resolve_layout(authority.project_root),
        config=bootstrap_config.load_config(authority.project_root, authority=authority),
    )


def prepare_for_runtime_read(project_root: Path) -> RuntimeReadContext:
    """Prepare read-only runtime context without creating state."""

    authority = resolve_runtime_authority_for_read(project_root)
    project_context = ProjectReadContext(
        authority=authority,
        project_root=authority.project_root,
        layout=project_state.resolve_layout(authority.project_root),
        config=bootstrap_config.load_config(authority.project_root),
    )
    return RuntimeReadContext(
        authority=authority,
        project_root=project_context.project_root,
        layout=project_context.layout,
        config=project_context.config,
        runtime_root=authority.runtime_root,
    )


def prepare_for_project_write(project_root: Path) -> ProjectWriteContext:
    """Prepare project write context, running migration and project dir setup."""

    authority = resolve_project_authority(project_root)
    project_state.migrate_legacy_paths(authority.project_root)
    project_state.ensure_project_dirs(authority.project_root)
    project_state.ensure_project_gitignore(authority.project_root)
    return ProjectWriteContext(
        authority=authority,
        project_root=authority.project_root,
        layout=project_state.resolve_layout(authority.project_root),
        config=bootstrap_config.load_config(authority.project_root, authority=authority),
    )


def prepare_for_runtime_write(project_root: Path) -> RuntimeWriteContext:
    """Prepare runtime write context, including UUID and runtime dirs."""

    authority = resolve_runtime_authority_for_write(project_root)
    project_state.migrate_legacy_paths(authority.project_root)
    project_state.ensure_project_dirs(authority.project_root)
    project_state.ensure_project_gitignore(authority.project_root)
    runtime_root = runtime_state.ensure_runtime_root(
        authority.project_root,
        authority=authority,
    )
    runtime_state.ensure_runtime_dirs(runtime_root)
    return RuntimeWriteContext(
        authority=authority.model_copy(update={"runtime_root": runtime_root}),
        project_root=authority.project_root,
        layout=project_state.resolve_layout(authority.project_root),
        config=bootstrap_config.load_config(authority.project_root, authority=authority),
        runtime_root=runtime_root,
    )


def build_spawn_entrypoint(
    prepared: RuntimeReadContext | RuntimeWriteContext,
    *,
    lifecycle: SpawnLifecycleService | None = None,
) -> SpawnEntryPoint:
    """Build the minimal shared carrier for spawn entrypoints."""

    return SpawnEntryPoint(
        context=_application_context_from_runtime(prepared),
        services=ApplicationServices(lifecycle=lifecycle),
    )


def build_spawn_application_service(
    prepared: RuntimeWriteContext,
    *,
    lifecycle: SpawnLifecycleService | None = None,
) -> SpawnApplicationService:
    """Build a spawn application service from the shared spawn entrypoint seam."""

    from meridian.lib.core.lifecycle import create_lifecycle_service
    from meridian.lib.core.spawn_service import SpawnApplicationService

    entrypoint = build_spawn_entrypoint(prepared, lifecycle=lifecycle)
    project_root = entrypoint.context.project_root
    runtime_root = entrypoint.context.runtime_root
    if project_root is None:
        raise ValueError("Spawn entrypoint is missing project root.")
    if runtime_root is None:
        raise ValueError("Spawn entrypoint is missing runtime root.")
    lifecycle_service = entrypoint.services.lifecycle or create_lifecycle_service(
        project_root,
        runtime_root,
    )
    return SpawnApplicationService(runtime_root, lifecycle_service)


def build_chat_entrypoint(
    prepared: RuntimeReadContext | RuntimeWriteContext,
    *,
    lifecycle: SpawnLifecycleService | None = None,
) -> ChatEntryPoint:
    """Build the minimal shared carrier for chat entrypoints."""

    return ChatEntryPoint(
        context=_application_context_from_runtime(prepared),
        services=ApplicationServices(lifecycle=lifecycle),
    )


def build_extension_entrypoint(
    prepared: RuntimeReadContext | RuntimeWriteContext,
    *,
    lifecycle: SpawnLifecycleService | None = None,
) -> ExtensionEntryPoint:
    """Build the minimal shared carrier for extension entrypoints."""

    return ExtensionEntryPoint(
        context=_application_context_from_runtime(prepared),
        services=ApplicationServices(lifecycle=lifecycle),
    )
