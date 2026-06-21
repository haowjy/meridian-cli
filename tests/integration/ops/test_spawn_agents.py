"""Integration coverage for spawn agent listing."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.ops.spawn.api import SpawnAgentsInput, spawn_agents_sync

pytestmark = pytest.mark.slow


def _seed_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (project_root / ".meridian").mkdir(parents=True)
    return project_root


def _seed_agent(project_root: Path, name: str, display_name: str) -> None:
    agents_dir = project_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{name}.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {display_name}",
                "model-policies:",
                "  - match: {alias: gpt55}",
                "    override: {effort: medium}",
                "---",
                "",
                "Profile body.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_spawn_agents_lists_seeded_profile_names(tmp_path: Path) -> None:
    project_root = _seed_project(tmp_path)
    _seed_agent(project_root, "coder", "Coder")
    _seed_agent(project_root, "reviewer", "Reviewer")

    output = spawn_agents_sync(SpawnAgentsInput(project_root=project_root.as_posix()))

    assert output.names == ("Coder", "Reviewer")
    assert output.format_text(None) == "Coder\nReviewer"
