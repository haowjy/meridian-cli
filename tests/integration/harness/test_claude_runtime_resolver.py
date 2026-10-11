from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.claude_runtime_resolver import resolve_claude_runtime
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver


def _write_manifest(plugin_dir: Path) -> None:
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")


@pytest.mark.parametrize("plugin_name", ["meridian-idle", "meridian-session-boundary"])
def test_claude_runtime_resolver_prefers_packaged_plugin(tmp_path: Path, plugin_name: str) -> None:
    package_root = tmp_path / "package"
    install_root = tmp_path / "install"
    package_plugin = package_root / plugin_name
    _write_manifest(package_plugin)
    _write_manifest(install_root / plugin_name)

    resolution = resolve_claude_runtime(
        package_runtime_root=package_root, install_root=install_root,
        plugin_names=(plugin_name,),
    )

    assert resolution.plugin_dirs == (str(package_plugin.resolve()),)
    assert resolution.warnings == ()


@pytest.mark.parametrize("available", ["package", "install", "symlink"])
def test_resolution_retains_paths_and_warnings_without_reconstructing_names(
    tmp_path: Path, available: str,
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    install_root = tmp_path / "install"
    name = "meridian-session-boundary"
    plugin = (install_root if available == "install" else package_root) / name
    if available == "symlink":
        target = tmp_path / "cache/version-123"
        _write_manifest(target)
        plugin.symlink_to(target, target_is_directory=True)
    else:
        _write_manifest(plugin)
    resolution = resolve_claude_runtime(
        plugin_names=(name, "meridian-idle"),
        package_runtime_root=package_root, install_root=install_root,
    )
    assert resolution.plugin_dirs == (str(plugin.resolve()),)
    assert len(resolution.warnings) == 1
    warning = resolution.warnings[0]
    assert "'meridian-idle'" in warning.message
    assert warning.detail == {
        "package_dir": str(package_root / "meridian-idle"),
        "install_dir": str(install_root / "meridian-idle"),
    }
    spec = ResolvedLaunchSpec(
        claude_runtime=resolution,
        permission_resolver=TieredPermissionResolver(config=PermissionConfig()),
    )
    # Reporting uses the captured outcome, not path basenames or another filesystem check.
    (plugin / ".claude-plugin/plugin.json").unlink()
    assert ClaudeAdapter().launch_spec_warnings(spec) == resolution.warnings


def test_claude_runtime_resolver_reports_all_missing_plugins(tmp_path: Path) -> None:
    resolution = resolve_claude_runtime(
        plugin_names=("meridian-session-boundary", "meridian-idle"),
        package_runtime_root=tmp_path / "package", install_root=tmp_path / "install",
    )
    assert resolution.plugin_dirs == ()
    assert len(resolution.warnings) == 2
    assert all(warning.code == "claude_runtime_missing" for warning in resolution.warnings)
