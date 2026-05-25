"""Integration tests for session search corpus scopes."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.ops.session_search import SessionSearchInput, session_search_sync
from meridian.lib.state import session_store


def _write_codex_rollout(
    *,
    home_root: Path,
    project_root: Path,
    session_id: str,
    assistant_text: str,
) -> None:
    sessions_root = home_root / ".codex" / "sessions" / "2026" / "04"
    sessions_root.mkdir(parents=True, exist_ok=True)
    rollout_path = sessions_root / f"rollout-2026-04-22T00-00-00-{session_id}.jsonl"
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


def test_session_search_workspace_scope_uses_runtime_evidence_not_repo_markers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home_root = tmp_path / "home"
    monkeypatch.setenv("HOME", home_root.as_posix())

    current_root = tmp_path / "current"
    current_root.mkdir()
    (current_root / "meridian.toml").write_text("", encoding="utf-8")

    workspace_root = tmp_path / "workspace-repo"
    workspace_root.mkdir()
    (current_root / "meridian.local.toml").write_text(
        "[workspace.docs]\npath = '../workspace-repo'\n",
        encoding="utf-8",
    )

    current_runtime = current_root / ".meridian"
    workspace_runtime = workspace_root / ".meridian"
    current_runtime.mkdir(parents=True, exist_ok=True)
    workspace_runtime.mkdir(parents=True, exist_ok=True)

    current_chat_id = session_store.start_session(
        current_runtime,
        harness="codex",
        harness_session_id="11111111-1111-1111-1111-111111111111",
        model="gpt-5.4-mini",
    )
    workspace_chat_id = session_store.start_session(
        workspace_runtime,
        harness="codex",
        harness_session_id="22222222-2222-2222-2222-222222222222",
        model="gpt-5.4-mini",
    )
    try:
        _write_codex_rollout(
            home_root=home_root,
            project_root=current_root,
            session_id="11111111-1111-1111-1111-111111111111",
            assistant_text="no match here",
        )
        _write_codex_rollout(
            home_root=home_root,
            project_root=workspace_root,
            session_id="22222222-2222-2222-2222-222222222222",
            assistant_text="workspace scope needle",
        )

        output = session_search_sync(
            SessionSearchInput(
                query="needle",
                project_root=current_root.as_posix(),
                workspace=True,
            )
        )
    finally:
        session_store.stop_session(current_runtime, current_chat_id)
        session_store.stop_session(workspace_runtime, workspace_chat_id)

    assert len(output.matches) == 1
    match = output.matches[0]
    assert match.corpus == workspace_root.as_posix()
    assert not (workspace_root / "meridian.toml").exists()
    assert not (workspace_root / ".git").exists()
    assert match.open_command.startswith("meridian session log --file ")
    assert "--segment 0 --around 1 --context 5" in match.open_command


def test_session_search_global_scope_includes_runtime_root(tmp_path: Path, monkeypatch) -> None:
    user_home = tmp_path / "meridian-home"
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setenv("HOME", (tmp_path / "home").as_posix())

    current_root = tmp_path / "current"
    current_root.mkdir()
    (current_root / "meridian.toml").write_text("", encoding="utf-8")

    runtime_root = user_home / "projects" / "orphan-one"
    runtime_root.mkdir(parents=True, exist_ok=True)
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="33333333-3333-3333-3333-333333333333",
        model="gpt-5.4-mini",
    )
    try:
        _write_codex_rollout(
            home_root=tmp_path / "home",
            project_root=runtime_root,
            session_id="33333333-3333-3333-3333-333333333333",
            assistant_text="global scope needle",
        )
        output = session_search_sync(
            SessionSearchInput(
                query="needle",
                project_root=current_root.as_posix(),
                global_scope=True,
            )
        )
    finally:
        session_store.stop_session(runtime_root, chat_id)

    assert len(output.matches) == 1
    assert output.matches[0].corpus == "runtime:orphan-one"
