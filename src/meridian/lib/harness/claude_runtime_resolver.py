"""Resolve requested Claude plugins and their warnings together, exactly once."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.launch.launch_types import ClaudeRuntimeResolution, CompositionWarning
from meridian.lib.state.user_paths import get_user_home


def _default_package_runtime_root() -> Path:
    return Path(__file__).resolve().parents[2] / "claude_runtime"


def _default_install_root() -> Path:
    return get_user_home() / "claude" / "mods"


def resolve_claude_runtime(
    *,
    plugin_names: tuple[str, ...],
    package_runtime_root: Path | None = None,
    install_root: Path | None = None,
) -> ClaudeRuntimeResolution:
    """Prefer packaged plugins, retaining missing-plugin diagnostics at resolution."""
    package_root = package_runtime_root or _default_package_runtime_root()
    installed_root = install_root or _default_install_root()
    directories: list[str] = []
    warnings: list[CompositionWarning] = []
    for name in plugin_names:
        package_dir = (package_root / name).resolve()
        install_dir = (installed_root / name).resolve()
        directory = next(
            (
                path
                for path in (package_dir, install_dir)
                if (path / ".claude-plugin" / "plugin.json").is_file()
            ),
            None,
        )
        if directory is not None:
            directories.append(str(directory))
        else:
            warnings.append(
                CompositionWarning(
                    code="claude_runtime_missing",
                    message=(
                        f"Meridian's Claude plugin {name!r} was not found; "
                        "its observations and features will be unavailable for this launch."
                    ),
                    detail={"package_dir": str(package_dir), "install_dir": str(install_dir)},
                )
            )
    return ClaudeRuntimeResolution(plugin_dirs=tuple(directories), warnings=tuple(warnings))
