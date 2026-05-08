"""Invocation context and service contracts for extension command execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from meridian.lib.service_context import (
    ApplicationContext,
    ApplicationServices,
    ExtensionEntryPoint,
)

if TYPE_CHECKING:
    from meridian.lib.config.settings import MeridianConfig
    from meridian.lib.core.lifecycle import SpawnLifecycleService
    from meridian.lib.core.spawn_service import SpawnApplicationService
    from meridian.lib.extensions.types import ExtensionSurface
    from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot


class SpawnServiceFactory(Protocol):
    """Factory contract for extension-facing spawn application services."""

    def __call__(self, entrypoint: ExtensionEntryPoint) -> SpawnApplicationService: ...


def _default_spawn_service_factory(entrypoint: ExtensionEntryPoint) -> SpawnApplicationService:
    from meridian.lib.bootstrap.services import build_spawn_application_service_from_entrypoint

    return build_spawn_application_service_from_entrypoint(entrypoint)


@dataclass(frozen=True)
class ExtensionSharedServices:
    """Shared service factory bundle extensions can consume."""

    spawn_service_factory: SpawnServiceFactory = _default_spawn_service_factory


class ExtensionCapability(StrEnum):
    """Capabilities that can be granted to extension command execution."""

    SUBPROCESS = "subprocess"
    KERNEL = "kernel"
    HITL = "hitl"


@dataclass(frozen=True)
class ExtensionCapabilities:
    """Granted capabilities for a command invocation."""

    subprocess: bool = False
    kernel: bool = False
    hitl: bool = False

    @classmethod
    def elevated(cls) -> ExtensionCapabilities:
        """Full capabilities for UI/HTTP contexts."""

        return cls(subprocess=True, kernel=True, hitl=True)

    @classmethod
    def denied(cls) -> ExtensionCapabilities:
        """No capabilities for CLI/MCP contexts."""

        return cls()

    def has(self, capability: str) -> bool:
        """Check if a capability is granted."""

        return bool(getattr(self, capability, False))


@dataclass(frozen=True)
class ExtensionInvocationContext:
    """Context available to extension command handlers."""

    caller_surface: ExtensionSurface
    authority: RuntimeAuthoritySnapshot | None
    project_uuid: str | None
    work_id: str | None
    work_path: Path | None
    spawn_id: str | None
    capabilities: ExtensionCapabilities
    request_id: str | None = None


class ExtensionInvocationContextBuilder:
    """Builder for ExtensionInvocationContext with surface-aware defaults."""

    def __init__(self, surface: ExtensionSurface) -> None:
        self._surface = surface
        self._authority: RuntimeAuthoritySnapshot | None = None
        self._project_uuid: str | None = None
        self._work_id: str | None = None
        self._work_path: Path | None = None
        self._spawn_id: str | None = None
        self._capabilities: ExtensionCapabilities | None = None
        self._request_id: str | None = None

    def with_authority(
        self,
        authority: RuntimeAuthoritySnapshot | None,
    ) -> ExtensionInvocationContextBuilder:
        self._authority = authority
        return self

    def with_project_uuid(self, uuid: str | None) -> ExtensionInvocationContextBuilder:
        self._project_uuid = uuid
        return self

    def with_work_id(self, work_id: str | None) -> ExtensionInvocationContextBuilder:
        self._work_id = work_id
        return self

    def with_work_path(self, path: Path | None) -> ExtensionInvocationContextBuilder:
        self._work_path = path
        return self

    def with_spawn_id(self, spawn_id: str | None) -> ExtensionInvocationContextBuilder:
        self._spawn_id = spawn_id
        return self

    def with_capabilities(
        self,
        caps: ExtensionCapabilities,
    ) -> ExtensionInvocationContextBuilder:
        self._capabilities = caps
        return self

    def with_request_id(self, request_id: str) -> ExtensionInvocationContextBuilder:
        self._request_id = request_id
        return self

    def build(self) -> ExtensionInvocationContext:
        """Build context with surface-appropriate capability defaults."""

        from meridian.lib.extensions.types import ExtensionSurface

        if self._capabilities is None:
            if self._surface in (ExtensionSurface.CLI, ExtensionSurface.MCP):
                caps = ExtensionCapabilities.denied()
            else:
                caps = ExtensionCapabilities.elevated()
        else:
            caps = self._capabilities

        resolved_work_path: Path | None = None
        if (
            self._work_id is not None
            and self._work_path is not None
            and self._work_path.exists()
        ):
            resolved_work_path = self._work_path

        return ExtensionInvocationContext(
            caller_surface=self._surface,
            authority=self._authority,
            project_uuid=self._project_uuid,
            work_id=self._work_id,
            work_path=resolved_work_path,
            spawn_id=self._spawn_id,
            capabilities=caps,
            request_id=self._request_id,
        )


@dataclass
class ExtensionCommandServices:
    """Services available to extension command handlers."""

    authority: RuntimeAuthoritySnapshot | None = None
    runtime_root: Path | None = None
    meridian_dir: Path | None = None
    application: ExtensionEntryPoint = field(default_factory=ExtensionEntryPoint)
    shared: ExtensionSharedServices = field(default_factory=ExtensionSharedServices)

    def resolved_project_root(self) -> Path | None:
        """Resolve project root from shared application/authority inputs."""

        project_root = self.application.context.project_root
        if project_root is not None:
            return project_root
        if self.authority is not None:
            return self.authority.project_root
        return None

    def resolved_runtime_root(self) -> Path | None:
        """Resolve runtime root from shared application/authority inputs."""

        runtime_root = self.application.context.runtime_root
        if runtime_root is not None:
            return runtime_root
        if self.authority is not None:
            return self.authority.runtime_root
        return self.runtime_root

    def require_project_root(self) -> Path:
        """Return project root or raise if authority is unavailable."""

        project_root = self.resolved_project_root()
        if project_root is None:
            raise ValueError("project_root not available")
        return project_root

    def require_runtime_root(self) -> Path:
        """Return runtime root or raise if authority is unavailable."""

        runtime_root = self.resolved_runtime_root()
        if runtime_root is None:
            raise ValueError("runtime_root not available")
        return runtime_root

    def build_spawn_service(self) -> SpawnApplicationService:
        """Build spawn service from shared application/service authority."""

        return self.shared.spawn_service_factory(self.application)


def build_extension_command_services(
    *,
    application: ExtensionEntryPoint | None = None,
    authority: RuntimeAuthoritySnapshot | None = None,
    project_root: Path | None = None,
    runtime_root: Path | None = None,
    config: MeridianConfig | None = None,
    lifecycle: SpawnLifecycleService | None = None,
    meridian_dir: Path | None = None,
    shared: ExtensionSharedServices | None = None,
) -> ExtensionCommandServices:
    """Build extension services with the shared application seam attached."""

    resolved_application = application or ExtensionEntryPoint(
        context=ApplicationContext(
            project_root=project_root,
            runtime_root=runtime_root,
            config=config,
            authority=authority,
        ),
        services=ApplicationServices(lifecycle=lifecycle),
    )
    resolved_authority = authority or resolved_application.context.authority
    resolved_runtime_root = (
        resolved_application.context.runtime_root
        or (resolved_authority.runtime_root if resolved_authority is not None else None)
        or runtime_root
    )

    return ExtensionCommandServices(
        authority=resolved_authority,
        runtime_root=resolved_runtime_root,
        meridian_dir=meridian_dir,
        application=resolved_application,
        shared=shared or ExtensionSharedServices(),
    )


__all__ = [
    "ExtensionCapabilities",
    "ExtensionCapability",
    "ExtensionCommandServices",
    "ExtensionInvocationContext",
    "ExtensionInvocationContextBuilder",
    "ExtensionSharedServices",
    "SpawnServiceFactory",
    "build_extension_command_services",
]
