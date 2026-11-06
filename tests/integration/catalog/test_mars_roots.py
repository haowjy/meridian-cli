"""Mars project and user package-root resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.catalog import mars_capabilities
from meridian.lib.catalog.mars_capabilities import mars_supports_fallback_root
from meridian.lib.catalog.mars_roots import (
    MarsRoots,
    ensure_user_mars_root,
    mars_content_roots,
    mars_fallback_active,
    mars_fallback_args,
    resolve_mars_roots,
)

if TYPE_CHECKING:
    import pytest


def test_resolve_mars_roots_prefers_project_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_home = tmp_path / "user-home"
    project = tmp_path / "project"
    project.mkdir()
    (project / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())

    roots = resolve_mars_roots(project)

    assert roots.primary == project.resolve()
    assert roots.scope == "project"
    assert roots.user == (user_home / "mars").resolve()


def test_resolve_mars_roots_walks_up_from_repo_subdirectory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_home = tmp_path / "user-home"
    repo = tmp_path / "repo"
    nested = repo / "src" / "package"
    nested.mkdir(parents=True)
    (repo / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())

    roots = resolve_mars_roots(nested)

    assert roots.primary == repo.resolve()
    assert roots.scope == "project"


def test_resolve_mars_roots_uses_user_root_for_bare_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_home = tmp_path / "user-home"
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())

    roots = resolve_mars_roots(bare)

    assert roots.primary == (user_home / "mars").resolve()
    assert roots.scope == "user"
    assert roots.user is None


def test_fallback_activation_is_shared_by_content_and_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "project"
    user = tmp_path / "user-home" / "mars"
    calls: list[str | None] = []
    monkeypatch.setattr(
        "meridian.lib.catalog.mars_roots.mars_supports_fallback_root",
        lambda executable: calls.append(executable) or True,
    )
    project_roots = MarsRoots(primary=primary, scope="project", user=user)
    user_roots = MarsRoots(primary=user, scope="user")

    assert mars_fallback_active(project_roots, executable="/fake/mars") == user
    assert mars_content_roots(project_roots, executable="/fake/mars") == (primary, user)
    assert mars_fallback_args(project_roots, executable="/fake/mars") == (
        "--fallback-root",
        user.as_posix(),
    )
    assert mars_fallback_active(user_roots, executable="/fake/mars") is None
    assert mars_content_roots(user_roots, executable="/fake/mars") == (user,)
    assert mars_fallback_args(user_roots, executable="/fake/mars") == ()
    assert calls == ["/fake/mars", "/fake/mars", "/fake/mars"]


def test_ensure_user_mars_root_initializes_only_user_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    user_root = user_home / "mars"
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        user_root.mkdir(parents=True)
        (user_root / "mars.toml").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    assert ensure_user_mars_root(executable="/fake/mars", run=fake_run) == user_root.resolve()
    assert calls == [["/fake/mars", "init", "--root", user_root.resolve().as_posix()]]
    # The caller's directory is where the user root must not litter.
    assert not any(bare.iterdir())


def test_fallback_root_capability_probe_is_cached_per_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["timeout"] == 10
        return subprocess.CompletedProcess(
            command,
            0,
            "Usage: mars build launch-bundle [OPTIONS]\n  --fallback-root <DIR>\n",
            "",
        )

    mars_supports_fallback_root.cache_clear()
    monkeypatch.setattr(mars_capabilities.subprocess, "run", fake_run)

    assert mars_supports_fallback_root("/fake/supported-mars")
    assert mars_supports_fallback_root("/fake/supported-mars")
    assert calls == [["/fake/supported-mars", "build", "launch-bundle", "--help"]]
