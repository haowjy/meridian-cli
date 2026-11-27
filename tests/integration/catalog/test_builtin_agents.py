from pathlib import Path

import pytest

from meridian.lib.catalog.agent import builtin_agents_dir, load_agent_profile


def _project_with_agents(root: Path, *stems: str) -> Path:
    agents = root / ".mars" / "agents"
    agents.mkdir(parents=True)
    (root / "mars.toml").write_text("[dependencies]\n", encoding="utf-8")
    for stem in stems:
        (agents / f"{stem}.md").write_text(
            f"---\nname: {stem}\ndescription: mine\n---\nMine.\n", encoding="utf-8"
        )
    return agents


def test_mars_agents_of_the_same_name_win_over_a_builtin_profile(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    assert load_agent_profile("inbox-router", bare).path == builtin_agents_dir() / "inbox-router.md"

    project = tmp_path / "project"
    own = _project_with_agents(project, "inbox-router") / "inbox-router.md"
    assert load_agent_profile("inbox-router", project).path == own.resolve()

    collided = tmp_path / "collided"
    _project_with_agents(collided, "inbox-router__a", "inbox-router__b")
    with pytest.raises(FileNotFoundError, match="inbox-router__a, inbox-router__b"):
        load_agent_profile("inbox-router", collided)
