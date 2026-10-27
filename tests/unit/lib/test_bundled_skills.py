"""Contracts for skills bundled with the Meridian package."""

from importlib.resources import files

from meridian.lib.bundled_skills import list_bundled_skills


def test_bundled_skills_are_valid_mars_provided_entries() -> None:
    skills = list_bundled_skills()
    skills_root = files("meridian.skills")

    assert skills
    for skill in skills:
        skill_dir = skills_root.joinpath(skill.name)
        assert skill.name.endswith("__meridian")
        assert not skill_dir.joinpath("resources", "BOOTSTRAP.md").is_file()
        assert not skill_dir.joinpath("variants").is_dir()
