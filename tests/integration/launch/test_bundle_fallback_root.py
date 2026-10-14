"""Fallback-root transport and bundle source provenance."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog import mars_roots
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.bundle_adapter import BundleRequest


@pytest.mark.parametrize(
    ("project_scope", "supported", "expects_fallback"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ],
)
def test_bundle_fallback_flag_requires_project_scope_and_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_scope: bool,
    supported: bool,
    expects_fallback: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    if project_scope:
        (project / "mars.toml").write_text("", encoding="utf-8")
    user_home = tmp_path / "home"
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setattr(bundle_adapter, "_resolve_mars_binary", lambda: "/fake/mars")
    monkeypatch.setattr(
        mars_roots,
        "mars_supports_fallback_root",
        lambda _executable: supported,
    )

    command = bundle_adapter._build_bundle_command(
        BundleRequest(agent=None, project_root=project)
    )

    fallback_args = ["--fallback-root", (user_home / "mars").resolve().as_posix()]
    if expects_fallback:
        assert command[-2:] == fallback_args
    else:
        assert "--fallback-root" not in command
