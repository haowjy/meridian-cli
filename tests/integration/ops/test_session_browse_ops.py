from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.ops.session_list import SessionListInput, session_list_sync
from meridian.lib.ops.session_reentry import Fork, Resume, resolve_session_reentry
from meridian.lib.ops.session_search import iter_session_subset_search
from meridian.lib.state import session_store, work_repository
from meridian.lib.state.paths import resolve_project_paths, resolve_project_runtime_root_for_write


def _project_roots(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex"]\n',
        encoding="utf-8",
    )
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return project_root, runtime_root


def _write_codex_rollout(
    *, home: Path, project_root: Path, session_id: str, text: str
) -> None:
    rollout_dir = home / ".codex" / "sessions" / "2026" / "08"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout = rollout_dir / f"rollout-2026-08-26T00-00-00-{session_id}.jsonl"
    rollout.write_text(
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
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        },
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_session_list_is_primary_only_live_first_and_capped(tmp_path: Path) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    project_state_dir = resolve_project_paths(project_root).root_dir
    work_repository.create_work_item(project_state_dir, "browse-feature")

    stopped_chat = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="11111111-1111-4111-8111-111111111111",
        model="gpt-stopped",
        agent="coder",
        kind="primary",
    )
    session_store.stop_session(runtime_root, stopped_chat)
    spawn_chat = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="22222222-2222-4222-8222-222222222222",
        model="gpt-spawn",
        kind="spawn",
    )
    live_chat = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="33333333-3333-4333-8333-333333333333",
        model="gpt-live",
        agent="reviewer",
        kind="primary",
        task_cwd=(tmp_path / "task").as_posix(),
    )
    session_store.update_session_work_id(runtime_root, live_chat, "browse-feature")
    try:
        output = session_list_sync(
            SessionListInput(project_root=project_root.as_posix(), limit=1)
        )
    finally:
        session_store.stop_session(runtime_root, live_chat)
        session_store.stop_session(runtime_root, spawn_chat)

    assert output.total_count == 2
    assert output.older_count == 1
    assert len(output.rows) == 1
    row = output.rows[0]
    assert row.chat_id == live_chat
    assert row.live is True
    assert row.reentry == Fork(live_chat)
    assert row.work_label == "browse-feature"
    assert "+1 older · raise --limit to see more" in output.format_text()


def test_session_reentry_rechecks_live_lease(tmp_path: Path) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="44444444-4444-4444-8444-444444444444",
        model="gpt-5.4",
        kind="primary",
    )

    assert resolve_session_reentry(project_root.as_posix(), chat_id) == Fork(chat_id)

    session_store.stop_session(runtime_root, chat_id)

    assert resolve_session_reentry(project_root.as_posix(), chat_id) == Resume(chat_id)


def test_subset_search_is_ordered_and_failure_isolated(
    tmp_path: Path, monkeypatch
) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", home.as_posix())
    matching_id = "55555555-5555-4555-8555-555555555555"
    other_id = "66666666-6666-4666-8666-666666666666"
    matching_chat = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=matching_id,
        model="gpt-5.4",
        kind="primary",
    )
    other_chat = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=other_id,
        model="gpt-5.4",
        kind="primary",
    )
    session_store.stop_session(runtime_root, matching_chat)
    session_store.stop_session(runtime_root, other_chat)
    _write_codex_rollout(
        home=home,
        project_root=project_root,
        session_id=matching_id,
        text="the session browse needle is here",
    )
    _write_codex_rollout(
        home=home,
        project_root=project_root,
        session_id=other_id,
        text="something unrelated",
    )

    steps = list(
        iter_session_subset_search(
            project_root=project_root.as_posix(),
            chat_ids=(other_chat, "c999", matching_chat),
            query="NEEDLE",
        )
    )

    assert [step.chat_id for step in steps] == [other_chat, "c999", matching_chat]
    assert steps[0].matched is False and steps[0].error is None
    assert steps[1].matched is False and steps[1].error
    assert steps[2].matched is True and steps[2].error is None
