"""Harness adapter ownership tests for non-trivial session detection."""

import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.claude import (
    ClaudeAdapter,
    _candidate_claude_project_dirs,
    project_slug,
)
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.opencode import OpenCodeAdapter
from meridian.lib.harness.session_detection import infer_harness_from_untracked_session_ref
from meridian.lib.launch.request import SessionRequest


@pytest.fixture(autouse=True)
def _clear_inherited_claude_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _write_codex_rollout(codex_home: Path, project_root: Path, session_id: str) -> Path:
    rollout_dir = codex_home / "sessions" / "2026" / "03" / "08"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout_path = rollout_dir / f"rollout-2026-03-08T12-00-00-{session_id}.jsonl"
    rollout_path.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": session_id, "cwd": project_root.as_posix()},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant"},
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return rollout_path


def _write_opencode_log(logs_dir: Path, project_root: Path, session_id: str, ts: str) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{session_id}.log"
    log_path.write_text(
        (
            f"INF {ts} +12ms service=session "
            f"id={session_id} directory={project_root.as_posix()} created\n"
        ),
        encoding="utf-8",
    )
    return log_path


def test_claude_adapter_detects_expected_project_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    old_session_id = str(uuid4())
    new_session_id = str(uuid4())
    project_dir = fake_home / ".claude" / "projects" / project_slug(project_root)
    project_dir.mkdir(parents=True)

    old_path = project_dir / f"{old_session_id}.jsonl"
    old_path.write_text(
        json.dumps({"type": "agent-setting", "sessionId": old_session_id}) + "\n",
        encoding="utf-8",
    )
    new_path = project_dir / f"{new_session_id}.jsonl"
    new_path.write_text(
        json.dumps({"type": "agent-setting", "sessionId": new_session_id}) + "\n",
        encoding="utf-8",
    )

    now = time.time()
    os.utime(old_path, (now - 10, now - 10))
    os.utime(new_path, (now, now))

    adapter = ClaudeAdapter()
    assert (
        adapter.detect_primary_session_id(
            project_root=project_root,
            started_at_epoch=now - 1,
            started_at_local_iso=None,
            expected_session_id=new_session_id,
        )
        == new_session_id
    )
    assert (
        adapter.owns_untracked_session(project_root=project_root, session_ref=new_session_id)
        is True
    )
    assert infer_harness_from_untracked_session_ref(project_root, new_session_id) == "claude"


def test_candidate_dirs_returns_exact_slug_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    child_cwd = project_root / ".meridian" / "spawns" / "p1"
    child_cwd.mkdir(parents=True)
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    exact_dir = fake_home / ".claude" / "projects" / project_slug(project_root)
    prefixed_child_dir = fake_home / ".claude" / "projects" / project_slug(child_cwd)
    exact_dir.mkdir(parents=True)
    prefixed_child_dir.mkdir(parents=True)

    assert _candidate_claude_project_dirs(project_root) == [exact_dir]


def test_claude_adapter_does_not_resolve_session_from_prefixed_child_project_slug(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    child_cwd = project_root / ".meridian" / "spawns" / "p1"
    child_cwd.mkdir(parents=True)
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    session_id = str(uuid4())
    child_project_dir = fake_home / ".claude" / "projects" / project_slug(child_cwd)
    child_project_dir.mkdir(parents=True)
    child_session_path = child_project_dir / f"{session_id}.jsonl"
    child_session_path.write_text(
        json.dumps({"type": "agent-setting", "sessionId": session_id}) + "\n",
        encoding="utf-8",
    )

    adapter = ClaudeAdapter()
    assert adapter.resolve_session_file(project_root=project_root, session_id=session_id) is None
    assert (
        adapter.owns_untracked_session(project_root=project_root, session_ref=session_id)
        is False
    )
    assert child_session_path.exists()


def test_claude_adapter_resolve_prefers_project_root_project_slug(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    child_cwd = project_root / ".meridian" / "spawns" / "p1"
    child_cwd.mkdir(parents=True)
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    session_id = str(uuid4())
    root_project_dir = fake_home / ".claude" / "projects" / project_slug(project_root)
    root_project_dir.mkdir(parents=True)
    root_session_path = root_project_dir / f"{session_id}.jsonl"
    root_session_path.write_text(
        json.dumps({"type": "agent-setting", "sessionId": session_id}) + "\n",
        encoding="utf-8",
    )

    child_project_dir = fake_home / ".claude" / "projects" / project_slug(child_cwd)
    child_project_dir.mkdir(parents=True)
    child_session_path = child_project_dir / f"{session_id}.jsonl"
    child_session_path.write_text(
        json.dumps({"type": "agent-setting", "sessionId": session_id}) + "\n",
        encoding="utf-8",
    )

    adapter = ClaudeAdapter()
    assert (
        adapter.resolve_session_file(project_root=project_root, session_id=session_id)
        == root_session_path
    )
    assert child_session_path.exists()


def test_claude_adapter_uses_claude_config_dir_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    configured_root = tmp_path / "configured-claude"
    monkeypatch.setenv("HOME", fake_home.as_posix())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", configured_root.as_posix())

    default_session_id = str(uuid4())
    override_session_id = str(uuid4())
    default_project_dir = fake_home / ".claude" / "projects" / project_slug(project_root)
    override_project_dir = configured_root / "projects" / project_slug(project_root)
    default_project_dir.mkdir(parents=True)
    override_project_dir.mkdir(parents=True)

    default_path = default_project_dir / f"{default_session_id}.jsonl"
    override_path = override_project_dir / f"{override_session_id}.jsonl"
    default_path.write_text(
        json.dumps({"type": "agent-setting", "sessionId": default_session_id}) + "\n",
        encoding="utf-8",
    )
    override_path.write_text(
        json.dumps({"type": "agent-setting", "sessionId": override_session_id}) + "\n",
        encoding="utf-8",
    )

    now = time.time()
    os.utime(default_path, (now, now))
    os.utime(override_path, (now - 5, now - 5))

    adapter = ClaudeAdapter()
    assert (
        adapter.detect_primary_session_id(
            project_root=project_root,
            started_at_epoch=now - 10,
            started_at_local_iso=None,
            expected_session_id=override_session_id,
        )
        == override_session_id
    )
    assert (
        adapter.resolve_session_file(project_root=project_root, session_id=override_session_id)
        == override_path
    )
    assert (
        adapter.owns_untracked_session(project_root=project_root, session_ref=override_session_id)
        is True
    )
    assert (
        adapter.owns_untracked_session(project_root=project_root, session_ref=default_session_id)
        is False
    )
    assert infer_harness_from_untracked_session_ref(project_root, override_session_id) == "claude"


@pytest.mark.parametrize(
    ("configured_root", "cwd_relative", "expected_suffix"),
    [
        pytest.param(
            "~/claude-root",
            False,
            ("home", "claude-root"),
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="HOME env var is not used by Path.expanduser() on Windows",
            ),
        ),
        ("relative-claude-root", True, ("repo", "relative-claude-root")),
    ],
)
def test_claude_prepare_prelaunch_normalizes_config_dir_for_env_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured_root: str,
    cwd_relative: bool,
    expected_suffix: tuple[str, ...],
) -> None:
    fake_home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    child_root = repo_root / "child"
    fake_home.mkdir(parents=True)
    child_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", fake_home.as_posix())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", configured_root)
    if cwd_relative:
        monkeypatch.chdir(repo_root)

    recorded_dirs: list[str] = []
    state = ClaudeAdapter().prepare_prelaunch(
        runtime_root=tmp_path,
        spawn_id=SpawnId("p-test"),
        session=SessionRequest(),
        child_cwd=child_root,
        child_env={},
        resolved_harness_session_id="session-1",
        record_effective_config_dir=recorded_dirs.append,
    )

    expected = str((tmp_path / Path(*expected_suffix)).resolve())
    assert state.env_overrides["CLAUDE_CONFIG_DIR"] == expected
    assert recorded_dirs == [expected]


def test_codex_adapter_owns_session_detection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    session_id = str(uuid4())
    rollout_path = _write_codex_rollout(fake_home / ".codex", project_root, session_id)
    now = time.time()
    os.utime(rollout_path, (now, now))

    adapter = CodexAdapter()
    assert (
        adapter.detect_primary_session_id(
            project_root=project_root,
            started_at_epoch=now - 1,
            started_at_local_iso=None,
        )
        == session_id
    )
    assert adapter.owns_untracked_session(project_root=project_root, session_ref=session_id) is True
    assert infer_harness_from_untracked_session_ref(project_root, session_id) == "codex"


def test_codex_adapter_uses_codex_home_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    codex_home_override = tmp_path / "codex-home"
    monkeypatch.setenv("HOME", fake_home.as_posix())
    monkeypatch.setenv("CODEX_HOME", codex_home_override.as_posix())

    default_session_id = str(uuid4())
    override_session_id = str(uuid4())
    default_rollout = _write_codex_rollout(fake_home / ".codex", project_root, default_session_id)
    override_rollout = _write_codex_rollout(codex_home_override, project_root, override_session_id)

    now = time.time()
    os.utime(default_rollout, (now, now))
    os.utime(override_rollout, (now - 5, now - 5))

    adapter = CodexAdapter()
    assert (
        adapter.detect_primary_session_id(
            project_root=project_root,
            started_at_epoch=now - 10,
            started_at_local_iso=None,
        )
        == override_session_id
    )
    assert (
        adapter.resolve_session_file(project_root=project_root, session_id=override_session_id)
        == override_rollout
    )
    assert (
        adapter.owns_untracked_session(project_root=project_root, session_ref=override_session_id)
        is True
    )
    assert (
        adapter.owns_untracked_session(project_root=project_root, session_ref=default_session_id)
        is False
    )
    assert infer_harness_from_untracked_session_ref(project_root, override_session_id) == "codex"


def test_opencode_adapter_owns_session_detection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())

    session_id = "session-123456"
    logs_dir = fake_home / ".local" / "share" / "opencode" / "log"
    log_path = _write_opencode_log(logs_dir, project_root, session_id, "2026-03-08T12:00:05")
    now = time.time()
    os.utime(log_path, (now, now))

    adapter = OpenCodeAdapter()
    assert (
        adapter.detect_primary_session_id(
            project_root=project_root,
            started_at_epoch=now - 1,
            started_at_local_iso="2026-03-08T12:00:00",
        )
        == session_id
    )
    assert adapter.owns_untracked_session(project_root=project_root, session_ref=session_id) is True
    assert infer_harness_from_untracked_session_ref(project_root, session_id) == "opencode"


def test_opencode_adapter_uses_xdg_data_home_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    fake_home = tmp_path / "home"
    xdg_data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("HOME", fake_home.as_posix())
    monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home.as_posix())

    default_session_id = "default-session-123456"
    override_session_id = "override-session-123456"
    default_log = _write_opencode_log(
        fake_home / ".local" / "share" / "opencode" / "log",
        project_root,
        default_session_id,
        "2026-03-08T12:00:10",
    )
    override_log = _write_opencode_log(
        xdg_data_home / "opencode" / "log",
        project_root,
        override_session_id,
        "2026-03-08T12:00:05",
    )

    now = time.time()
    os.utime(default_log, (now, now))
    os.utime(override_log, (now - 5, now - 5))

    adapter = OpenCodeAdapter()
    assert (
        adapter.detect_primary_session_id(
            project_root=project_root,
            started_at_epoch=now - 10,
            started_at_local_iso="2026-03-08T12:00:00",
        )
        == override_session_id
    )
    assert (
        adapter.owns_untracked_session(project_root=project_root, session_ref=override_session_id)
        is True
    )
    assert (
        adapter.owns_untracked_session(project_root=project_root, session_ref=default_session_id)
        is False
    )
    assert infer_harness_from_untracked_session_ref(project_root, override_session_id) == "opencode"
