"""Minimal typed service/context carriers for application entrypoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.config.settings import MeridianConfig

if TYPE_CHECKING:
    from meridian.lib.core.lifecycle import SpawnLifecycleService
    from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot


@dataclass(frozen=True)
class ApplicationContext:
    """Shared authority inputs an application surface may receive."""

    project_root: Path | None = None
    runtime_root: Path | None = None
    config: MeridianConfig | None = None
    authority: RuntimeAuthoritySnapshot | None = None


@dataclass(frozen=True)
class ApplicationServices:
    """Shared service handles an application surface may receive."""

    lifecycle: SpawnLifecycleService | None = None


@dataclass(frozen=True)
class ApplicationEntryPoint:
    """Typed carrier shared by chat, spawn, and extension entrypoints."""

    context: ApplicationContext = field(default_factory=ApplicationContext)
    services: ApplicationServices = field(default_factory=ApplicationServices)


SpawnEntryPoint = ApplicationEntryPoint

ExtensionEntryPoint = ApplicationEntryPoint


__all__ = [
    "ApplicationContext",
    "ApplicationEntryPoint",
    "ApplicationServices",

    "ExtensionEntryPoint",
    "SpawnEntryPoint",
]
