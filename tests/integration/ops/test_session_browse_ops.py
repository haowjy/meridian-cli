from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.ops.session_list import SessionListInput, session_list_sync
from meridian.lib.ops.session_reentry import Blocked, Fork, Resume, resolve_session_reentry
from meridian.lib.state import primary_meta, session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.support.launch_paths import launch_paths


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


def _write_codex_rollout(*, home: Path, project_root: Path, session_id: str, text: str) -> None:
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


def test_session_reentry_rechecks_live_lease(tmp_path: Path) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="44444444-4444-4444-8444-444444444444",
        native_store=(tmp_path / "codex-sessions").as_posix(),
        model="gpt-5.4",
        kind="primary",
    ).chat_id

    assert resolve_session_reentry(project_root.as_posix(), chat_id) == Fork(chat_id)

    session_store.stop_session(runtime_root, chat_id)

    assert resolve_session_reentry(project_root.as_posix(), chat_id) == Resume(chat_id)


def test_list_and_reentry_refuse_primary_metadata_as_binding(tmp_path: Path) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    harness_session_id = "45454545-4545-4545-8545-454545454545"
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.4",
        kind="primary",
        spawn_id="p42",
    ).chat_id
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id=chat_id,
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        kind="primary",
        prompt="primary",
    )
    primary_meta.write_primary_metadata(
        launch_paths(runtime_root, "p42"),
        primary_meta.PrimaryMetadata(
            managed_backend=True,
            harness_session_id=harness_session_id,
        ),
    )

    live_listing = session_list_sync(SessionListInput(project_root=project_root.as_posix()))
    live_row = next(row for row in live_listing.rows if row.chat_id == chat_id)
    assert isinstance(live_row.reentry, Blocked)
    assert isinstance(resolve_session_reentry(project_root.as_posix(), chat_id), Blocked)

    session_store.stop_session(runtime_root, chat_id)

    stopped_listing = session_list_sync(SessionListInput(project_root=project_root.as_posix()))
    stopped_row = next(row for row in stopped_listing.rows if row.chat_id == chat_id)
    assert isinstance(stopped_row.reentry, Blocked)
    assert isinstance(resolve_session_reentry(project_root.as_posix(), chat_id), Blocked)
