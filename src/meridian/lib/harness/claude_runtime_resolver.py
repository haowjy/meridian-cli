"""Resolve individual bundled Claude runtime plugins without coupling their policies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meridian.lib.launch.launch_types import CompositionWarning
from meridian.lib.state.user_paths import get_user_home

_IDLE_PLUGIN_NAME: Final[str] = "meridian-idle"


@dataclass(frozen=True)
class ClaudeRuntimeResolution:
    """Resolved plugin directories for one Claude launch."""

    plugin_dirs: tuple[str, ...]


def _default_package_runtime_root() -> Path:
    return Path(__file__).resolve().parents[2] / "claude_runtime"


def _default_install_root() -> Path:
    return get_user_home() / "claude" / "mods"


def missing_claude_runtime_warning(
    *,
    plugin_name: str = _IDLE_PLUGIN_NAME,
    package_runtime_root: Path | None = None,
    install_root: Path | None = None,
) -> CompositionWarning:
    """Describe runtime paths omitted from a Claude launch."""

    package_dir = (
        (package_runtime_root or _default_package_runtime_root()) / plugin_name
    ).resolve()
    install_dir = ((install_root or _default_install_root()) / plugin_name).resolve()
    return CompositionWarning(
        code="claude_runtime_missing",
        message=(
            f"Meridian's Claude plugin {plugin_name!r} was not found; "
            "its observations and features will be unavailable for this launch."
        ),
        detail={
            "package_dir": str(package_dir),
            "install_dir": str(install_dir),
        },
    )


def resolve_claude_runtime(
    *,
    plugin_name: str = _IDLE_PLUGIN_NAME,
    package_runtime_root: Path | None = None,
    install_root: Path | None = None,
) -> ClaudeRuntimeResolution:
    """Resolve one plugin, preferring package data over install data."""

    package_dir = (
        (package_runtime_root or _default_package_runtime_root()) / plugin_name
    ).resolve()
    install_dir = ((install_root or _default_install_root()) / plugin_name).resolve()

    for candidate in (package_dir, install_dir):
        if (candidate / ".claude-plugin" / "plugin.json").is_file():
            return ClaudeRuntimeResolution(plugin_dirs=(str(candidate),))

    return ClaudeRuntimeResolution(plugin_dirs=())


__all__ = [
    "ClaudeRuntimeResolution",
    "missing_claude_runtime_warning",
    "resolve_claude_runtime",
]
