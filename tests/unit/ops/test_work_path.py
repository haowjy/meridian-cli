from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from meridian.lib.ops.context import WorkPathInput, work_path_sync


def _setup_ambient_project(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-work-path", encoding="utf-8")

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p42")
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    return project_root


def test_work_path_rejects_scope_escape(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _setup_ambient_project(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="escapes scope directory"):
        work_path_sync(WorkPathInput(relpath="../escape.md"))


@pytest.mark.parametrize(
    "relpath",
    [
        "/tmp/abs.md",  # POSIX-absolute (drive-relative on Windows)
        "\\rooted.md",  # rooted on Windows
        "C:\\windows\\abs.md",  # Windows drive-absolute
        "C:relative.md",  # Windows drive-relative
    ],
)
def test_work_path_rejects_absolute_relpath(
    tmp_path: Path, monkeypatch: MonkeyPatch, relpath: str
) -> None:
    _setup_ambient_project(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="must be relative"):
        work_path_sync(WorkPathInput(relpath=relpath))
