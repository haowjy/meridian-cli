"""One resolution preserves package precedence, fallback and symlink-safe warnings."""

from pathlib import Path

from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.claude_runtime_resolver import resolve_claude_runtime
from meridian.lib.launch.launch_types import ResolvedLaunchSpec


def _write_manifest(plugin_dir: Path) -> None:
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")


def test_resolution_preserves_plugin_selection_and_warnings(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    installed = tmp_path / "install"
    cached = tmp_path / "cache/version-123"
    boundary = "meridian-session-boundary"
    _write_manifest(cached)
    (package / boundary).symlink_to(cached, target_is_directory=True)
    _write_manifest(installed / boundary)
    _write_manifest(installed / "meridian-idle")
    resolution = resolve_claude_runtime(
        plugin_names=(boundary, "meridian-idle", "not-installed"),
        package_runtime_root=package, install_root=installed,
    )
    assert resolution.plugin_dirs == (str(cached), str(installed / "meridian-idle"))
    assert len(resolution.warnings) == 1
    warning = resolution.warnings[0]
    assert "'not-installed'" in warning.message
    assert warning.detail == {
        "package_dir": str(package / "not-installed"),
        "install_dir": str(installed / "not-installed"),
    }
    spec = ResolvedLaunchSpec(
        claude_runtime=resolution,
    )
    (cached / ".claude-plugin/plugin.json").unlink()
    assert ClaudeAdapter().launch_spec_warnings(spec) == resolution.warnings
