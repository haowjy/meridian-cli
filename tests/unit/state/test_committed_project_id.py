"""A committed project ID is refused unless it reads back unchanged from its runtime root."""

import json
from pathlib import Path

import pytest

from meridian.lib.state.project_identity import get_committed_project_id


def _project(tmp_path: Path, project_id: str) -> Path:
    (tmp_path / "meridian.toml").write_text(
        f"[project]\nid = {json.dumps(project_id)}\n", encoding="utf-8"
    )
    return tmp_path


@pytest.mark.parametrize("project_id", ["mellow-hollow-brood", "team/proj"])
def test_id_in_the_form_is_accepted(tmp_path: Path, project_id: str) -> None:
    assert get_committed_project_id(_project(tmp_path, project_id)) == project_id


@pytest.mark.parametrize(
    "project_id",
    [
        "a//b",
        "team/",
        "/team",
        "/",
        ".",
        "..",
        "../escape",
        "team/./proj",
        ".locks",
        "team/.locks",
        "a\x1bb",
        "a" * 256,
    ],
)
def test_id_outside_the_form_is_refused(tmp_path: Path, project_id: str) -> None:
    root = _project(tmp_path, project_id)

    with pytest.raises(ValueError) as refusal:
        get_committed_project_id(root)

    message = str(refusal.value)
    assert (root / "meridian.toml").as_posix() in message
    assert repr(project_id) in message
    assert "joined by `/`" in message
