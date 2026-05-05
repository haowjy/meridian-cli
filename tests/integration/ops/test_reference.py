from pathlib import Path

import pytest

from meridian.lib.ops import reference
from meridian.lib.ops.reference import resolve_session_reference
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root


def _state_root(project_root: Path) -> Path:
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _seed_session(
    runtime_root: Path,
    *,
    chat_id: str,
    harness_session_id: str,
    extra_harness_session_ids: tuple[str, ...] = (),
    work_id: str | None = None,
    harness: str = "codex",
    execution_cwd: str | None = None,
    claude_config_dir: str | None = None,
) -> str:
    resolved_chat_id = session_store.start_session(
        runtime_root,
        harness=harness,
        harness_session_id=harness_session_id,
        model="gpt-5.4",
        chat_id=chat_id,
        agent="coder",
        skills=("skill-a", "skill-b"),
        execution_cwd=execution_cwd,
        claude_config_dir=claude_config_dir,
    )
    if work_id is not None:
        session_store.update_session_work_id(runtime_root, resolved_chat_id, work_id)
    for candidate in extra_harness_session_ids:
        session_store.update_session_harness_id(runtime_root, resolved_chat_id, candidate)
    session_store.stop_session(runtime_root, resolved_chat_id)
    return resolved_chat_id


def _seed_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    chat_id: str,
    harness_session_id: str | None,
    harness: str = "codex",
    kind: str = "child",
    execution_cwd: str | None = None,
    started_at: str | None = None,
    claude_config_dir: str | None = None,
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id=chat_id,
        model="gpt-5.3-codex",
        agent="coder",
        skills=("skill-c",),
        harness=harness,
        kind=kind,
        prompt="seed prompt",
        work_id="w-spawn",
        harness_session_id=harness_session_id,
        execution_cwd=execution_cwd,
        started_at=started_at,
    )
    if claude_config_dir is not None:
        spawn_store.update_spawn(runtime_root, spawn_id, claude_config_dir=claude_config_dir)


def test_resolve_session_reference_uses_latest_chat_session_id(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    chat_id = _seed_session(
        runtime_root,
        chat_id="c41",
        harness_session_id="session-1",
        extra_harness_session_ids=("session-2", "session-3"),
        work_id="w-chat",
    )

    resolved = resolve_session_reference(project_root, chat_id)

    assert resolved.harness_session_id == "session-3"
    assert resolved.harness == "codex"
    assert resolved.source_chat_id == "c41"
    assert resolved.source_model == "gpt-5.4"
    assert resolved.source_agent == "coder"
    assert resolved.source_skills == ("skill-a", "skill-b")
    assert resolved.source_work_id == "w-chat"
    assert resolved.tracked is True
    assert resolved.warning is None


def test_resolve_session_reference_for_spawn_id_reads_spawn_metadata(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p7",
        chat_id="c7",
        harness_session_id="spawn-session-7",
        claude_config_dir="/tmp/spawn-claude",
    )

    resolved = resolve_session_reference(project_root, "p7")

    assert resolved.harness_session_id == "spawn-session-7"
    assert resolved.harness == "codex"
    assert resolved.source_chat_id == "c7"
    assert resolved.source_model == "gpt-5.3-codex"
    assert resolved.source_agent == "coder"
    assert resolved.source_skills == ("skill-c",)
    assert resolved.source_work_id == "w-spawn"
    assert resolved.tracked is True
    assert resolved.source_claude_config_dir == "/tmp/spawn-claude"
    assert resolved.warning is None


def test_resolve_spawn_ref_prefers_direct_spawn_id_match(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p7", chat_id="c7", harness_session_id="spawn-session-7")

    resolved = reference.resolve_spawn_ref(runtime_root, "p7")

    assert resolved is not None
    assert str(resolved) == "p7"


def test_resolve_spawn_ref_uses_latest_chat_match_by_started_at(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p3",
        chat_id="c213",
        harness_session_id="session-new",
        started_at="2026-01-02T00:00:00Z",
    )
    _seed_spawn(
        runtime_root,
        spawn_id="p9",
        chat_id="c213",
        harness_session_id="session-old",
        started_at="2026-01-01T00:00:00Z",
    )

    resolved = reference.resolve_spawn_ref(runtime_root, "c213")

    assert resolved is not None
    assert str(resolved) == "p3"


def test_resolve_session_reference_for_spawn_uses_execution_cwd_when_recorded(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    execution_cwd = str(tmp_path / "custom-cwd")
    _seed_spawn(
        runtime_root,
        spawn_id="p9",
        chat_id="c9",
        harness_session_id="spawn-session-9",
        execution_cwd=execution_cwd,
    )

    resolved = resolve_session_reference(project_root, "p9")

    assert resolved.source_execution_cwd == execution_cwd


def test_resolve_session_reference_for_legacy_claude_spawn_infers_log_dir(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p10",
        chat_id="c10",
        harness_session_id="claude-session-10",
        harness="claude",
        kind="child",
        execution_cwd=None,
    )

    resolved = resolve_session_reference(project_root, "p10")

    assert resolved.source_execution_cwd == str(
        resolve_project_runtime_root(project_root) / "spawns" / "p10"
    )


def test_resolve_session_reference_allows_spawn_without_harness_session_id(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p8", chat_id="c8", harness_session_id=None)

    resolved = resolve_session_reference(project_root, "p8")

    assert resolved.harness_session_id is None
    assert resolved.harness == "codex"
    assert resolved.tracked is True
    assert resolved.missing_harness_session_id is True


def test_resolve_session_reference_for_chat_uses_recorded_execution_cwd(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    execution_cwd = str(tmp_path / "chat-cwd")
    chat_id = _seed_session(
        runtime_root,
        chat_id="c42",
        harness_session_id="session-42",
        work_id="w-chat",
        harness="claude",
        execution_cwd=execution_cwd,
        claude_config_dir="/tmp/session-claude",
    )

    resolved = resolve_session_reference(project_root, chat_id)

    assert resolved.source_execution_cwd == execution_cwd
    assert resolved.source_claude_config_dir == "/tmp/session-claude"


def test_resolve_session_reference_falls_back_to_untracked_raw_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()

    monkeypatch.setattr(
        reference,
        "infer_harness_from_untracked_session_ref",
        lambda *_args, **_kwargs: "claude",
    )

    resolved = resolve_session_reference(project_root, "raw-session-id")

    assert resolved.harness_session_id == "raw-session-id"
    assert resolved.harness == "claude"
    assert resolved.source_chat_id is None
    assert resolved.source_model is None
    assert resolved.source_agent is None
    assert resolved.source_skills == ()
    assert resolved.source_work_id is None
    assert resolved.source_execution_cwd is None
    assert resolved.tracked is False
    assert resolved.missing_harness_session_id is False
    assert (
        resolved.warning
        == "Session 'raw-session-id' is not tracked yet; "
        "resuming with the provided harness session id."
    )


def test_resolve_session_reference_missing_chat_id_falls_back_to_untracked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()

    monkeypatch.setattr(
        reference,
        "infer_harness_from_untracked_session_ref",
        lambda *_args, **_kwargs: "opencode",
    )

    resolved = resolve_session_reference(project_root, "c999")

    assert resolved.harness_session_id == "c999"
    assert resolved.harness == "opencode"
    assert resolved.tracked is False
