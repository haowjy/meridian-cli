"""A runtime root's project ID is its whole path under ``projects/``."""

from pathlib import Path

import pytest

from meridian.lib.state.runtime_root import runtime_root_project_id
from meridian.lib.state.user_paths import get_project_home


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())


@pytest.mark.parametrize("project_id", ["mellow-hollow-brood", "team/proj"])
def test_committed_id_is_the_path_under_projects(project_id: str) -> None:
    assert runtime_root_project_id(get_project_home(project_id)) == project_id


def test_adhoc_root_and_a_root_outside_projects_have_no_id(tmp_path: Path) -> None:
    assert runtime_root_project_id(get_project_home("adhoc.proj.0123456789ab")) is None
    assert runtime_root_project_id(tmp_path / "elsewhere" / "proj") is None
    assert runtime_root_project_id(get_project_home("x").parent) is None
