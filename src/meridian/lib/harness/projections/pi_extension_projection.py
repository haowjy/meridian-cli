"""Meridian-owned Pi extension projection helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meridian.lib.harness.pi_paths import (
    resolve_meridian_pi_extension_root,
    resolve_pi_default_user_extension_dir,
)

_EXTENSION_SOURCE_ROOT_OVERRIDE: Final[str] = "MERIDIAN_PI_EXTENSION_SOURCE_ROOT"

_BACKGROUND_TASKS_EXTENSION_RELATIVE_PATH: Final[tuple[str, str]] = (
    "background-tasks",
    "background-tasks/index.js",
)


class PiExtensionProjectionError(RuntimeError):
    """Raised when required Pi extension artifacts cannot be projected."""


@dataclass(frozen=True)
class PiExtensionLaunchProfile:
    """Resolved extension toggles for one Pi launch."""

    background_tasks_enabled: bool
    spawn_watch_enabled: bool
    interactive: bool

    def bundle_enabled(self) -> bool:
        """Whether the unified background-tasks bundle should load."""

        return self.background_tasks_enabled or self.spawn_watch_enabled


def resolve_pi_spawn_watch_entrypoint() -> tuple[str, ...]:
    """Legacy alias — spawn-watch merged into background-tasks."""

    return resolve_pi_background_tasks_entrypoint()


def resolve_pi_lifecycle_extension_entrypoint() -> tuple[str, ...]:
    """Legacy alias — lifecycle bundle merged into background-tasks."""

    return resolve_pi_background_tasks_entrypoint()


def resolve_pi_background_tasks_entrypoint() -> tuple[str, ...]:
    """Resolve the Meridian background-tasks Pi extension entrypoint."""

    return (_resolve_bundle_entrypoint(*_BACKGROUND_TASKS_EXTENSION_RELATIVE_PATH),)


def resolve_pi_extension_entrypoints(
    profile: PiExtensionLaunchProfile,
) -> tuple[str, ...]:
    """Resolve Meridian ``-e`` bundle paths for one launch."""

    if not profile.bundle_enabled():
        return ()
    return resolve_pi_background_tasks_entrypoint()


def resolve_pi_all_extension_entrypoints() -> tuple[str, ...]:
    """Resolve the default Meridian Pi bundle (background-tasks only)."""

    return resolve_pi_extension_entrypoints(
        PiExtensionLaunchProfile(
            background_tasks_enabled=True,
            spawn_watch_enabled=True,
            interactive=True,
        )
    )


def discover_pi_extension_entrypoints_under(root: Path) -> tuple[str, ...]:
    """Discover Pi ``-e`` entrypoints (``<dir>/index.js``) under one directory."""

    if not root.is_dir():
        return ()

    discovered: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        entrypoint = child / "index.js"
        if entrypoint.is_file():
            discovered.append(str(entrypoint.resolve()))
    return tuple(discovered)


def resolve_extra_pi_extension_entrypoints(
    extra_extension_paths: tuple[Path, ...],
) -> tuple[str, ...]:
    """Scan configured extra extension roots when ``load_all_pi_extensions`` is true."""

    entrypoints: list[str] = []
    for root in extra_extension_paths:
        entrypoints.extend(discover_pi_extension_entrypoints_under(root))
    return tuple(entrypoints)


def _resolve_bundle_entrypoint(extension_name: str, relative_path: str) -> str:
    source_root = _resolve_extension_source_root()
    source_path = source_root / relative_path
    if source_path.is_file():
        return str(source_path.resolve())

    install_root = resolve_meridian_pi_extension_root()
    install_path = install_root / extension_name / "index.js"
    if install_path.is_file():
        return str(install_path.resolve())

    raise PiExtensionProjectionError(
        "Missing Pi extension artifact: "
        f"{source_path}. Build Pi extensions first "
        "(cd src/meridian/pi_runtime && npm run build:extensions) "
        f"or install bundles under {install_root}."
    )


def _resolve_extension_source_root() -> Path:
    override = os.environ.get(_EXTENSION_SOURCE_ROOT_OVERRIDE)
    if override:
        return Path(override).expanduser().resolve()

    return (
        Path(__file__).resolve().parents[3]
        / "pi_runtime"
        / "dist"
        / "extensions"
    )


def default_extra_extension_path() -> Path:
    """Default user Pi extension dir (used only when ``load_all_pi_extensions``)."""

    return resolve_pi_default_user_extension_dir()


__all__ = [
    "PiExtensionLaunchProfile",
    "PiExtensionProjectionError",
    "default_extra_extension_path",
    "discover_pi_extension_entrypoints_under",
    "resolve_extra_pi_extension_entrypoints",
    "resolve_pi_all_extension_entrypoints",
    "resolve_pi_background_tasks_entrypoint",
    "resolve_pi_extension_entrypoints",
    "resolve_pi_lifecycle_extension_entrypoint",
    "resolve_pi_spawn_watch_entrypoint",
]
