"""Mars project and user package-root resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.catalog.mars_roots import ensure_user_mars_root, resolve_mars_roots

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


def test_resolve_mars_roots_treats_user_root_itself_as_user_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_home = tmp_path / "user-home"
    user_root = user_home / "mars"
    user_root.mkdir(parents=True)
    (user_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())

    roots = resolve_mars_roots(user_root)

    assert roots.primary == user_root.resolve()
    assert roots.scope == "user"
    assert roots.user is None


def test_ensure_user_mars_root_initializes_only_user_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    user_root = user_home / "mars"
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        user_root.mkdir(parents=True)
        (user_root / "mars.toml").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    assert ensure_user_mars_root(executable="/fake/mars", run=fake_run) == user_root.resolve()
    assert calls == [["/fake/mars", "init", "--root", user_root.resolve().as_posix()]]
    assert not any(bare.iterdir())
