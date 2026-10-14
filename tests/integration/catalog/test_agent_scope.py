"""Agent discovery across project and user Mars roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog.agent import load_agent_profile
from meridian.lib.catalog.skill import scan_skills


def _write_agent(root: Path, name: str) -> None:
    agents = root / ".mars" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\nProfile.\n",
        encoding="utf-8",
    )


def _write_skill(root: Path, name: str, body: str) -> None:
    skill = root / ".mars" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_project_agent_wins_over_user_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    project = tmp_path / "project"
    project.mkdir()
    (project / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setattr(
        "meridian.lib.catalog.mars_roots.mars_supports_fallback_root",
        lambda _executable: False,
    )
    _write_agent(user_home / "mars", "reviewer")
    _write_agent(project, "reviewer")

    profile = load_agent_profile("reviewer", project_root=project)

    assert profile.path == (project / ".mars" / "agents" / "reviewer.md").resolve()


def test_user_agent_inside_project_reports_install_here_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    project = tmp_path / "project"
    project.mkdir()
    (project / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setattr(
        "meridian.lib.catalog.mars_roots.mars_supports_fallback_root",
        lambda _executable: False,
    )
    _write_agent(user_home / "mars", "reviewer")

    with pytest.raises(FileNotFoundError) as exc_info:
        load_agent_profile("reviewer", project_root=project)

    assert str(exc_info.value) == (
        f"Agent 'reviewer' found in user packages ({user_home / 'mars'}); "
        "add it here: meridian mars add <source>"
    )


def test_user_agent_inside_project_loads_when_mars_supports_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    project = tmp_path / "project"
    project.mkdir()
    (project / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setattr(
        "meridian.lib.catalog.mars_roots.mars_supports_fallback_root",
        lambda _executable: True,
    )
    _write_agent(user_home / "mars", "reviewer")

    profile = load_agent_profile("reviewer", project_root=project)

    assert profile.path == (user_home / "mars/.mars/agents/reviewer.md").resolve()


def test_missing_user_agent_reports_user_install_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())

    with pytest.raises(FileNotFoundError) as exc_info:
        load_agent_profile("reviewer", project_root=bare)

    assert str(exc_info.value) == (
        f"Agent 'reviewer' not found in user packages ({user_home / 'mars'}). "
        "Run 'meridian mars --user add <source>'."
    )


def test_skills_discover_project_then_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    project = tmp_path / "project"
    project.mkdir()
    (project / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setattr(
        "meridian.lib.catalog.mars_roots.mars_supports_fallback_root",
        lambda _executable: True,
    )
    _write_skill(user_home / "mars", "shared", "user")
    _write_skill(user_home / "mars", "user-only", "user-only")
    _write_skill(project, "shared", "project")

    skills = scan_skills(project)

    assert [skill.name for skill in skills] == ["shared", "user-only"]
    assert skills[0].body.strip() == "project"
