from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.harness.claude_runtime_resolver import resolve_claude_runtime


def _write_manifest(plugin_dir: Path) -> None:
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")


@pytest.mark.parametrize("plugin_name", ["meridian-idle", "meridian-session-boundary"])
def test_claude_runtime_resolver_prefers_packaged_plugin(tmp_path: Path, plugin_name: str) -> None:
    package_root = tmp_path / "package"
    install_root = tmp_path / "install"
    package_plugin = package_root / plugin_name
    install_plugin = install_root / plugin_name
    _write_manifest(package_plugin)
    _write_manifest(install_plugin)

    resolution = resolve_claude_runtime(
        package_runtime_root=package_root,
        install_root=install_root,
        plugin_name=plugin_name,
    )

    assert resolution.plugin_dirs == (str(package_plugin.resolve()),)


def test_claude_runtime_resolver_returns_no_dirs_when_plugin_is_missing(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    install_root = tmp_path / "install"

    resolution = resolve_claude_runtime(
        package_runtime_root=package_root,
        install_root=install_root,
    )

    assert resolution.plugin_dirs == ()
