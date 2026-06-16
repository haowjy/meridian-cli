from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from meridian.lib.ops.context import WorkPathInput, work_path_sync
from meridian.lib.state.paths import resolve_ambient_work_dir
from meridian.lib.state.work_store import SCOPE_HANDOFFS_DIRNAME, SCOPE_PROMPTS_DIRNAME


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


def test_work_path_materializes_parent_and_returns_absolute_path(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = _setup_ambient_project(tmp_path, monkeypatch)
    expected = resolve_ambient_work_dir(project_root, "p42") / SCOPE_PROMPTS_DIRNAME / "fix.md"

    output = work_path_sync(WorkPathInput(relpath="prompts/fix.md"))

    assert output.path == expected.as_posix()
    assert expected.parent.is_dir()
    assert not expected.exists()


def test_work_path_creates_handoffs_bucket(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = _setup_ambient_project(tmp_path, monkeypatch)
    expected_parent = resolve_ambient_work_dir(project_root, "p42") / SCOPE_HANDOFFS_DIRNAME

    output = work_path_sync(WorkPathInput(relpath="handoffs/ctx.md"))

    assert output.path.endswith("handoffs/ctx.md")
    assert expected_parent.is_dir()


def test_work_path_rejects_scope_escape(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _setup_ambient_project(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="escapes scope directory"):
        work_path_sync(WorkPathInput(relpath="../escape.md"))


def test_work_path_rejects_absolute_relpath(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _setup_ambient_project(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="must be relative"):
        work_path_sync(WorkPathInput(relpath="/tmp/abs.md"))
