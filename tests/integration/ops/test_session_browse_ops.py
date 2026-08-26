from __future__ import annotations

import json
from pathlib import Path

import meridian.lib.ops.session_list as session_list_module
from meridian.lib.ops.session_list import SessionListInput, session_list_sync
from meridian.lib.ops.session_reentry import Blocked, Fork, Resume, resolve_session_reentry
from meridian.lib.ops.session_search import iter_session_subset_search
from meridian.lib.state import primary_meta, session_store, spawn_store, work_repository
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
    assert "(1 of 2 shown — use --limit to see more)" in output.format_text()


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


def test_list_and_reentry_share_recorded_primary_metadata_recovery(tmp_path: Path) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    harness_session_id = "45454545-4545-4545-8545-454545454545"
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.4",
        kind="primary",
    )
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
        runtime_root / "spawns" / "p42",
        primary_meta.PrimaryMetadata(harness_session_id=harness_session_id),
    )

    live_listing = session_list_sync(SessionListInput(project_root=project_root.as_posix()))
    live_row = next(row for row in live_listing.rows if row.chat_id == chat_id)
    assert live_row.reentry == Fork(chat_id)
    assert resolve_session_reentry(project_root.as_posix(), chat_id) == Fork(chat_id)

    session_store.stop_session(runtime_root, chat_id)

    stopped_listing = session_list_sync(SessionListInput(project_root=project_root.as_posix()))
    stopped_row = next(row for row in stopped_listing.rows if row.chat_id == chat_id)
    assert stopped_row.reentry == Resume(chat_id)
    assert resolve_session_reentry(project_root.as_posix(), chat_id) == Resume(chat_id)


def test_recorded_primary_spawn_id_avoids_global_spawn_recovery_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.4",
        kind="primary",
        spawn_id="p42",
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id=chat_id,
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        kind="primary",
        prompt="primary",
        harness_session_id="42424242-4242-4242-8242-424242424242",
    )
    backfilled = session_store.get_session_record(runtime_root, chat_id)
    assert backfilled is not None
    assert backfilled.spawn_id == "p42"

    def fail_global_scan(*_args, **_kwargs):
        raise AssertionError("direct primary relationship should avoid list_spawns")

    monkeypatch.setattr(spawn_store, "list_spawns", fail_global_scan)
    try:
        listing = session_list_sync(
            SessionListInput(project_root=project_root.as_posix(), limit=1)
        )
        assert listing.rows[0].reentry == Fork(chat_id)
        assert resolve_session_reentry(project_root.as_posix(), chat_id) == Fork(chat_id)
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_recorded_primary_spawn_without_harness_id_does_not_scan_globally(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.4",
        kind="primary",
        spawn_id="p42",
    )
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
    assert session_store.get_session_record(runtime_root, chat_id) is not None

    def fail_global_scan(*_args, **_kwargs):
        raise AssertionError("a canonical primary row must bound failed recovery")

    monkeypatch.setattr(spawn_store, "list_spawns", fail_global_scan)
    try:
        listing = session_list_sync(
            SessionListInput(project_root=project_root.as_posix(), limit=1)
        )
        assert isinstance(listing.rows[0].reentry, Blocked)
        assert isinstance(resolve_session_reentry(project_root.as_posix(), chat_id), Blocked)
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_spawn_publication_invalidates_negative_historical_backfill(tmp_path: Path) -> None:
    _project_root, runtime_root = _project_roots(tmp_path)
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.4",
        kind="primary",
    )
    initial = session_store.get_session_record(runtime_root, chat_id)
    assert initial is not None and initial.spawn_id is None

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id=chat_id,
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        kind="primary",
        prompt="primary",
        harness_session_id="42424242-4242-4242-8242-424242424242",
    )

    refreshed = session_store.get_session_record(runtime_root, chat_id)
    assert refreshed is not None
    assert refreshed.spawn_id == "p42"


def test_list_and_reentry_block_when_all_recorded_ids_are_missing(tmp_path: Path) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="",
        model="gpt-5.4",
        kind="primary",
    )
    try:
        listing = session_list_sync(SessionListInput(project_root=project_root.as_posix()))
        row = next(row for row in listing.rows if row.chat_id == chat_id)
        assert isinstance(row.reentry, Blocked)
        assert isinstance(resolve_session_reentry(project_root.as_posix(), chat_id), Blocked)
    finally:
        session_store.stop_session(runtime_root, chat_id)


def test_session_list_scans_spawn_state_once_for_missing_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    chat_ids = [
        session_store.start_session(
            runtime_root,
            harness="codex",
            harness_session_id="",
            model="gpt-5.4",
            kind="primary",
        )
        for _ in range(3)
    ]
    real_list_spawns = spawn_store.list_spawns
    scan_count = 0

    def counting_list_spawns(*args, **kwargs):
        nonlocal scan_count
        scan_count += 1
        return real_list_spawns(*args, **kwargs)

    monkeypatch.setattr(spawn_store, "list_spawns", counting_list_spawns)
    try:
        output = session_list_sync(
            SessionListInput(project_root=project_root.as_posix())
        )
    finally:
        for chat_id in chat_ids:
            session_store.stop_session(runtime_root, chat_id)

    assert {row.chat_id for row in output.rows} == set(chat_ids)
    assert scan_count == 1


def test_session_list_enriches_only_the_visible_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    project_state_dir = resolve_project_paths(project_root).root_dir
    chat_ids: list[str] = []
    for index in range(3):
        work_id = f"browse-page-{index}"
        work_repository.create_work_item(project_state_dir, work_id)
        chat_id = session_store.start_session(
            runtime_root,
            harness="codex",
            harness_session_id=f"{index + 1:08d}-1111-4111-8111-111111111111",
            model="gpt-5.4",
            kind="primary",
        )
        session_store.update_session_work_id(runtime_root, chat_id, work_id)
        if index < 2:
            session_store.stop_session(runtime_root, chat_id)
        chat_ids.append(chat_id)

    recovered_batches: list[tuple[str, ...]] = []
    real_recover = session_list_module.recover_recorded_chat_harness_session_ids

    def recording_recover(runtime_root: Path, sessions):
        recovered_batches.append(tuple(record.chat_id for record in sessions))
        return real_recover(runtime_root, sessions)

    monkeypatch.setattr(
        session_list_module,
        "recover_recorded_chat_harness_session_ids",
        recording_recover,
    )

    try:
        output = session_list_sync(
            SessionListInput(project_root=project_root.as_posix(), limit=1)
        )
    finally:
        session_store.stop_session(runtime_root, chat_ids[-1])

    assert output.total_count == 3
    assert [row.chat_id for row in output.rows] == [chat_ids[-1]]
    assert recovered_batches == [(chat_ids[-1],)]


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
    session_store.get_session_records(runtime_root, {matching_chat, other_chat})
    record_scan_count = 0
    spawn_scan_count = 0
    real_get_session_records = session_store.get_session_records
    real_list_spawns = spawn_store.list_spawns

    def counting_get_session_records(*args, **kwargs):
        nonlocal record_scan_count
        record_scan_count += 1
        return real_get_session_records(*args, **kwargs)

    def counting_list_spawns(*args, **kwargs):
        nonlocal spawn_scan_count
        spawn_scan_count += 1
        return real_list_spawns(*args, **kwargs)

    monkeypatch.setattr(session_store, "get_session_records", counting_get_session_records)
    monkeypatch.setattr(spawn_store, "list_spawns", counting_list_spawns)

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
    assert record_scan_count == 1
    assert spawn_scan_count == 0


def test_subset_search_recovers_history_when_recorded_primary_spawn_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", home.as_posix())
    harness_session_id = "77777777-7777-4777-8777-777777777777"
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=harness_session_id,
        model="gpt-5.4",
        kind="primary",
        spawn_id="p404",
    )
    session_store.stop_session(runtime_root, chat_id)
    _write_codex_rollout(
        home=home,
        project_root=project_root,
        session_id=harness_session_id,
        text="",
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p43",
        chat_id=chat_id,
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="related child",
    )
    (runtime_root / "spawns" / "p43" / "history.jsonl").write_text(
        json.dumps(
            {
                "event_type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "legacy history needle"}
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    steps = list(
        iter_session_subset_search(
            project_root=project_root.as_posix(),
            chat_ids=(chat_id,),
            query="needle",
        )
    )

    assert len(steps) == 1
    assert steps[0].matched is True
    assert steps[0].error is None
