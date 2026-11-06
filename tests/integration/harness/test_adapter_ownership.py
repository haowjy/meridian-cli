"""Harness-native ownership and exact source resolution."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.claude_sessions import project_slug
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.opencode import OpenCodeAdapter


@pytest.fixture(autouse=True)
def _clear_inherited_claude_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def test_codex_transcript_resolution_refuses_duplicate_exact_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session_id = str(uuid4())
    sessions_root = tmp_path / "codex" / "sessions"
    for timestamp in ("2026-01-01T00-00-00", "2026-01-02T00-00-00"):
        path = sessions_root / "2026" / timestamp / f"rollout-{timestamp}-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    adapter = CodexAdapter()
    with pytest.raises(NativeSessionUnavailable) as caught:
        adapter.resolve_session_file(
            project_root=tmp_path,
            session_id=session_id,
            config_root_hint=sessions_root,
        )
    assert caught.value.reason == "ambiguous_native_file"


def test_claude_transcript_resolution_is_pinned_to_recorded_project_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    session_id = str(uuid4())
    recorded_store = tmp_path / "recorded" / "projects" / project_slug(project_root)
    ambient_store = tmp_path / "ambient" / "projects" / project_slug(project_root)
    for store in (recorded_store, ambient_store):
        store.mkdir(parents=True)
        (store / f"{session_id}.jsonl").write_text(
            json.dumps({"sessionId": session_id}) + "\n",
            encoding="utf-8",
        )

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ambient"))
    adapter = ClaudeAdapter()
    assert (
        adapter.resolve_native_session_file(
            session_id=session_id,
            native_store=recorded_store,
        )
        == recorded_store / f"{session_id}.jsonl"
    )
    (recorded_store / f"{session_id}.jsonl").unlink()
    project_root = tmp_path / "another-cwd"
    decoy = tmp_path / "ambient" / "projects" / project_slug(project_root)
    decoy.mkdir(parents=True)
    (decoy / f"{session_id}.jsonl").write_text("{}\n")
    assert (
        adapter.resolve_native_session_file(
            session_id=session_id,
            native_store=recorded_store,
        )
        is None
    )


def test_opencode_transcript_resolution_uses_exact_storage_store(
    tmp_path: Path,
) -> None:
    session_id = "ses_native_exact"
    storage_root = tmp_path / "storage"
    expected = storage_root / "session" / f"{session_id}.json"
    expected.parent.mkdir(parents=True)
    expected.write_text("{}", encoding="utf-8")
    unrelated = storage_root / "session" / "ses_unrelated.json"
    unrelated.write_text("{}", encoding="utf-8")

    adapter = OpenCodeAdapter()
    assert (
        adapter.resolve_session_file(
            project_root=tmp_path,
            session_id=session_id,
            config_root_hint=storage_root,
        )
        == expected
    )

    duplicate = storage_root / "session_diff" / f"{session_id}.json"
    duplicate.parent.mkdir()
    duplicate.write_text("{}", encoding="utf-8")
    with pytest.raises(NativeSessionUnavailable) as caught:
        adapter.resolve_session_file(
            project_root=tmp_path,
            session_id=session_id,
            config_root_hint=storage_root,
        )
    assert caught.value.reason == "ambiguous_native_file"


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
        adapter.owns_untracked_session(project_root=project_root, session_ref=session_id) is False
    )
    assert child_session_path.exists()
