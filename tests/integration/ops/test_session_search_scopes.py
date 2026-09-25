"""Integration tests for session search corpus scopes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.harness.claude import project_slug
from meridian.lib.ops.session_search import SessionSearchInput, session_search_sync
from meridian.lib.state import session_store
from meridian.lib.state.user_paths import get_project_home


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
    (current_root / "meridian.toml").write_text(
        '[project]\nid = "current-project"\n', encoding="utf-8"
    )

    workspace_root = tmp_path / "workspace-repo"
    workspace_root.mkdir()
    (workspace_root / "meridian.toml").write_text(
        '[project]\nid = "workspace-project"\n', encoding="utf-8"
    )
    (current_root / "meridian.local.toml").write_text(
        "[workspace.docs]\npath = '../workspace-repo'\n",
        encoding="utf-8",
    )

    current_runtime = get_project_home("current-project")
    workspace_runtime = get_project_home("workspace-project")
    current_runtime.mkdir(parents=True, exist_ok=True)
    workspace_runtime.mkdir(parents=True, exist_ok=True)

    current_chat_id = session_store.start_session(
        current_runtime,
        harness="codex",
        harness_session_id="11111111-1111-1111-1111-111111111111",
        native_store=(home_root / ".codex" / "sessions").as_posix(),
        model="gpt-5.4-mini",
    )
    workspace_chat_id = session_store.start_session(
        workspace_runtime,
        harness="codex",
        harness_session_id="22222222-2222-2222-2222-222222222222",
        native_store=(home_root / ".codex" / "sessions").as_posix(),
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
    assert (workspace_root / "meridian.toml").is_file()
    assert not (workspace_root / ".git").exists()
    assert f"meridian session log {workspace_chat_id} " in match.open_command
    assert "--file" not in match.open_command
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
        native_store=((tmp_path / "home") / ".codex" / "sessions").as_posix(),
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


def test_session_search_corpus_resolves_tracked_claude_canonical_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_home = tmp_path / "meridian-home"
    home = tmp_path / "home"
    runtime_root = user_home / "projects" / "orphan-one"
    runtime_root.mkdir(parents=True)
    current_root = tmp_path / "current"
    current_root.mkdir()
    (current_root / "meridian.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setenv("HOME", home.as_posix())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", (tmp_path / "unrelated-overlay").as_posix())

    session_id = "claude-corpus-session"
    project_dir = home / ".claude" / "projects" / project_slug(runtime_root)
    project_dir.mkdir(parents=True)
    (project_dir / f"{session_id}.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"sessionId": session_id}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "corpus canonical needle"}]
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    chat_id = session_store.start_session(
        runtime_root,
        harness="claude",
        harness_session_id=session_id,
        model="claude-opus",
        claude_config_dir=(tmp_path / "recorded-overlay").as_posix(),
        native_store=str(project_dir),
    )
    try:
        output = session_search_sync(
            SessionSearchInput(
                query="canonical needle",
                project_root=current_root.as_posix(),
                global_scope=True,
            )
        )
    finally:
        session_store.stop_session(runtime_root, chat_id)

    assert len(output.matches) == 1
    assert output.matches[0].corpus == "runtime:orphan-one"


def test_large_native_transcript_is_rebuild_only(tmp_path: Path, monkeypatch) -> None:
    from meridian.lib.state import spawn_store
    from meridian.lib.state.history import ingest_portable_history
    from meridian.lib.state.history_index import HistoryIndex

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "meridian.toml").write_text('[project]\nid = "large-history"\n')
    root = get_project_home("large-history")
    key = spawn_store.start_spawn(
        root, chat_id="c1", harness="codex", model="test", agent="", prompt="query"
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    ingest_portable_history(
        root,
        key,
        iter(
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "early needle"}]},
                }
            ]
        ),
    )
    store = tmp_path / "native"
    store.mkdir()
    sid = "11111111-1111-4111-8111-111111111111"
    path = store / f"{sid}.jsonl"
    path.write_text(
        json.dumps({"sessionId": sid})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {"content": "early needle"},
            }
        )
        + "\n"
    )
    session_store.start_session(
        root,
        harness="claude",
        harness_session_id=sid,
        native_store=str(store),
        model="test",
        chat_id="c1",
    )
    padding = json.dumps({"type": "padding", "data": "x" * 4096}) + "\n"
    with path.open("a") as handle:
        for _ in range(16640):
            handle.write(padding)
    assert path.stat().st_size > 64 * 1024 * 1024
    HistoryIndex(root).rebuild()
    result = session_search_sync(SessionSearchInput(query="needle", project_root=str(project)))
    assert not result.matches
    assert not result.truncated and not result.complete
    assert result.sources_not_searched == 1
    assert "incomplete" in result.format_text()


def test_browse_subset_search_keeps_unbound_legacy_history_loose(tmp_path, monkeypatch):
    from meridian.lib.ops.session_archive import archive_history
    from meridian.lib.ops.session_search import iter_session_subset_search
    from meridian.lib.state import spawn_store
    from meridian.lib.state.history import ingest_portable_history

    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "meridian.toml").write_text('[project]\nid="subset"\n')
    root = get_project_home("subset")
    chat = session_store.start_session(
        root, harness="codex", harness_session_id=None, model="test", kind="primary"
    )
    key = str(
        spawn_store.start_spawn(
            root,
            chat_id=chat,
            harness="codex",
            model="test",
            agent="",
            prompt="hello",
            kind="primary",
        )
    )
    session_store.update_session_spawn_id(root, chat, key)
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    ingest_portable_history(
        root,
        key,
        iter(
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "portable needle"}]},
                },
            ]
        ),
    )
    session_store.stop_session(root, chat)
    row = spawn_store.get_spawn(root, key)
    assert row is not None
    loose = list(
        iter_session_subset_search(project_root=str(project), chat_ids=[chat], query="needle")
    )
    assert len(loose) == 1 and not loose[0].matched
    assert loose[0].error == f"unbound: no verified native session for {chat}"
    archived = archive_history(root, destination=tmp_path / "zips", refs=(key,), apply=True)
    assert not archived.reclaimed
    assert not archived.archives
    assert any("no exact native source is bound" in error for error in archived.errors)
    steps = list(
        iter_session_subset_search(
            project_root=str(project),
            chat_ids=[str(row.history_id), "c999"],
            query="needle",
        )
    )
    assert not steps[0].matched and "unbound" in (steps[0].error or "")
    assert not steps[1].matched and steps[1].error


@pytest.mark.parametrize("recorded_harness_id", [True, False])
def test_damaged_metadata_index_only_affects_work_scoped_search(
    tmp_path, monkeypatch, recorded_harness_id
):
    from meridian.lib.ops.reference import resolve_session_reference
    from meridian.lib.state import spawn_store
    from meridian.lib.state.history_index import HistoryIndex

    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "meridian.toml").write_text('[project]\nid="damaged"\n')
    root = get_project_home("damaged")
    chat = session_store.start_session(
        root,
        harness="codex",
        harness_session_id="11111111-1111-1111-1111-111111111111" if recorded_harness_id else None,
        model="test",
        kind="primary",
    )
    if not recorded_harness_id:
        # Deliberately unlinked primary: recover native ID and launch metadata from files.
        key = str(
            spawn_store.start_spawn(
                root,
                chat_id=chat,
                model="test",
                harness="codex",
                kind="primary",
                agent="",
                prompt="hello",
                harness_session_id="11111111-1111-1111-1111-111111111111",
            )
        )
        spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    session_store.stop_session(root, chat)
    expected = resolve_session_reference(project, chat, runtime_root=root)
    index = HistoryIndex(root)
    index.rebuild()
    index.path.write_bytes(b"not a sqlite database")  # offline: no live connections
    assert resolve_session_reference(project, chat, runtime_root=root) == expected
    result = session_search_sync(SessionSearchInput(query="needle", project_root=str(project)))
    assert result.complete  # Corpus keys come from the store, not metadata.
    index.rebuild(reset=True)
    (index.directory / "pending/GENERATION").write_text("invalid generation")
    result = session_search_sync(
        SessionSearchInput(query="needle", work_id="work", project_root=str(project))
    )
    assert not result.complete and result.errors
