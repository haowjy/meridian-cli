"""``-C`` into another project must not carry the caller's identity along."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.cli.main import _directory_env_scope
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.launch.cwd import inheritable_task_dir


def _project(root: Path, project_id: str) -> Path:
    root.mkdir(parents=True)
    (root / "meridian.toml").write_text(f'[project]\nid = "{project_id}"\n', encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("target_id", "keeps_caller_identity"),
    [("proj_other", False), ("proj_caller", True)],
)
def test_directory_scope_keeps_caller_identity_only_within_the_same_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_id: str,
    keeps_caller_identity: bool,
) -> None:
    caller = _project(tmp_path / "caller", "proj_caller")
    target = _project(tmp_path / "target", target_id)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", caller.as_posix())
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p7")
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "c7")
    monkeypatch.setenv("MERIDIAN_WORK_ID", "caller-work")
    monkeypatch.setenv("MERIDIAN_TASK_DIR", (tmp_path / "caller-checkout").as_posix())

    with _directory_env_scope(target, directory_explicit=True):
        context = ResolvedContext.from_environment()
        if keeps_caller_identity:
            assert context.spawn_id == "p7"
        else:
            assert not (context.spawn_id or context.chat_id or context.work_id)
            assert inheritable_task_dir(target, None) is None
