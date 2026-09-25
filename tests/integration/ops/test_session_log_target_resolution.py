"""Session log target resolution — detection preference, non-mutation, read-only contracts.

Tests that resolve_session_log_target reads state without reconciliation side-effects,
that detected transcripts take precedence without persisting the detected ID, and
that missing-transcript detection failures are not persisted.

# qa-validated: test-suite-redesign
"""

import json
from pathlib import Path

import pytest

from meridian.lib.ops.reference import resolve_session_reference
from meridian.lib.ops.session_target import resolve_session_log_target
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


def _write_codex_rollout(
    *,
    sessions_root: Path,
    project_root: Path,
    session_id: str,
    assistant_text: str,
) -> Path:
    rollout_dir = sessions_root / "2026" / "04"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout_path = rollout_dir / f"rollout-2026-04-22T00-00-00-{session_id}.jsonl"
    rollout_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": session_id, "cwd": project_root.as_posix()},
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": assistant_text}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return rollout_path


def test_identity_free_raw_harness_reference_resolves_without_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home.as_posix())
    session_id = "78f02237-df5f-43fe-a6e5-929f98287877"
    rollout = _write_codex_rollout(
        sessions_root=codex_home / "sessions",
        project_root=project_root,
        session_id=session_id,
        assistant_text="stateless transcript",
    )

    reference = resolve_session_reference(project_root, session_id)
    log_target = resolve_session_log_target(
        ref=session_id,
        file_path=None,
        project_root=project_root,
        runtime_root=None,
    )

    assert not (project_root / "meridian.toml").exists()
    assert reference.harness_session_id == session_id
    assert reference.harness == "codex"
    assert not reference.tracked
    assert log_target.session_id == session_id
    assert log_target.file_path == rollout


def test_resolve_target_chat_not_found_preserves_missing_chat_error(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError) as exc:
        resolve_session_log_target(
            ref="c999",
            file_path=None,
            project_root=project_root,
            runtime_root=runtime_root,
        )
    assert str(exc.value) == "Chat 'c999' not found"


def test_resolve_target_spawn_id_uses_read_only_lookup_without_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home.as_posix())
    session_id = "78f02237-df5f-43fe-a6e5-929f98287877"
    _write_codex_rollout(
        sessions_root=codex_home / "sessions",
        project_root=project_root,
        session_id=session_id,
        assistant_text="spawn transcript",
    )

    session_store.start_session(
        runtime_root, harness="codex", harness_session_id=session_id, model="test",
        chat_id="c1", spawn_id="p1", native_store=str(codex_home / "sessions"),
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="hello",
        spawn_id="p1",
        harness_session_id=session_id,
    )
    state_path = runtime_root / "spawns" / "p1" / "state.json"
    before_state = state_path.read_text(encoding="utf-8")

    def _unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("reconciliation should not run for read-only target resolution")

    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_spawns", _unexpected)
    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_active_spawn", _unexpected)
    monkeypatch.setattr("meridian.lib.ops.spawn.query.read_spawn_row", _unexpected)

    resolved = resolve_session_log_target(
        ref="p1",
        file_path=None,
        project_root=project_root,
        runtime_root=runtime_root,
    )

    assert resolved.session_id == session_id
    assert resolved.source == "codex transcript"
    assert state_path.read_text(encoding="utf-8") == before_state


def test_resolve_target_chat_id_uses_read_only_lookup_without_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home.as_posix())
    session_id = "6f2c95c5-f617-4e4d-80ab-d98f3270bcaf"
    _write_codex_rollout(
        sessions_root=codex_home / "sessions",
        project_root=project_root,
        session_id=session_id,
        assistant_text="chat transcript",
    )

    session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=session_id,
        model="gpt-5.4",
        chat_id="c1", spawn_id="p1", native_store=str(codex_home / "sessions"),
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p1",
        chat_id="c1",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="do thing",
        harness_session_id=session_id,
        status="running",
    )
    state_path = runtime_root / "spawns" / "p1" / "state.json"
    before_state = state_path.read_text(encoding="utf-8")

    def _unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("reconciliation should not run for read-only target resolution")

    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_spawns", _unexpected)
    monkeypatch.setattr("meridian.lib.state.reaper.reconcile_active_spawn", _unexpected)
    monkeypatch.setattr("meridian.lib.ops.spawn.query.read_spawn_row", _unexpected)

    resolved = resolve_session_log_target(
        ref="c1",
        file_path=None,
        project_root=project_root,
        runtime_root=runtime_root,
    )

    assert resolved.session_id == session_id
    assert resolved.source == "codex transcript"
    assert state_path.read_text(encoding="utf-8") == before_state


@pytest.mark.parametrize("native_id", ["", "missing-native-id"])
def test_chat_target_never_detects_a_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_id: str,
) -> None:
    from meridian.lib.core.types import HarnessId
    from meridian.lib.harness.registry import get_default_harness_registry
    from meridian.lib.ops.session_target import NativeSessionUnavailable

    root = tmp_path / "repo"
    root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(root)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-native-store"))
    adapter = get_default_harness_registry().get_subprocess_harness(HarnessId.CLAUDE)

    def forbidden_detection(**_kwargs: object) -> None:
        pytest.fail("exact chat resolution must not call a detector")

    monkeypatch.setattr(adapter, "detect_primary_session_id", forbidden_detection)
    chat_id = session_store.start_session(
        runtime_root, harness="claude", harness_session_id=native_id, model="test",
    )
    try:
        with pytest.raises(NativeSessionUnavailable) as caught:
            resolve_session_log_target(
                ref=chat_id, file_path=None, project_root=root, runtime_root=runtime_root,
            )
        assert caught.value.reason == "unbound"
        assert chat_id in str(caught.value)
        assert session_store.get_session_harness_id(runtime_root, chat_id) == (native_id or None)
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_tracked_claude_hint_cannot_replace_missing_native_store(tmp_path: Path) -> None:
    from meridian.lib.core.native_identity import NativeSessionUnavailable
    from meridian.lib.harness.claude_sessions import project_slug

    root = tmp_path / "repo"
    root.mkdir()
    runtime = resolve_project_runtime_root_for_write(root)
    config = tmp_path / "config"
    native = config / "projects" / project_slug(root) / "native-id.jsonl"
    native.parent.mkdir(parents=True)
    native.write_text('{"sessionId":"native-id","type":"user"}\n')
    chat = session_store.start_session(
        runtime, harness="claude", harness_session_id="native-id",
        model="test", claude_config_dir=str(config),
    )
    with pytest.raises(NativeSessionUnavailable) as caught:
        resolve_session_log_target(
            ref=chat, file_path=None, project_root=root, runtime_root=runtime,
        )
    assert caught.value.reason == "unbound"
    record = session_store.get_session_record(runtime, chat)
    assert record is not None and record.native_store is None
