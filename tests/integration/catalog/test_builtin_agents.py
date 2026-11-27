from pathlib import Path

from meridian.lib.catalog.agent import builtin_agents_dir, load_agent_profile


def test_project_agent_replaces_a_builtin_profile_of_the_same_name(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    assert load_agent_profile("inbox-router", bare).path == builtin_agents_dir() / "inbox-router.md"

    project = tmp_path / "project"
    agents = project / ".mars" / "agents"
    agents.mkdir(parents=True)
    (project / "mars.toml").write_text("[dependencies]\n", encoding="utf-8")
    own = agents / "inbox-router.md"
    own.write_text("---\nname: inbox-router\ndescription: mine\n---\nMine.\n", encoding="utf-8")
    assert load_agent_profile("inbox-router", project).path == own.resolve()
