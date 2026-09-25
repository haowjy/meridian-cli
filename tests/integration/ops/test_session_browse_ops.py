from __future__ import annotations

import json
from pathlib import Path

import pytest

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
        output = session_list_sync(SessionListInput(project_root=project_root.as_posix(), limit=1))
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


def test_session_list_breaks_equal_timestamp_ties_by_chat_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_store, "utc_now_iso", lambda: "2026-09-01T00:00:00Z")
    project_root, runtime_root = _project_roots(tmp_path)
    chat_ids = [
        session_store.start_session(
            runtime_root,
            harness="codex",
            harness_session_id=f"{index:08d}-1111-4111-8111-111111111111",
            model="gpt-5.4",
            kind="primary",
        )
        for index in range(1, 4)
    ]
    try:
        for chat_id in chat_ids:
            session_store.stop_session(runtime_root, chat_id)

        records = session_store.get_session_records(runtime_root, set(chat_ids))
        assert len({record.stopped_at for record in records}) == 1

        output = session_list_sync(SessionListInput(project_root=project_root.as_posix(), limit=1))
    finally:
        for chat_id in chat_ids:
            session_store.stop_session(runtime_root, chat_id)

    assert [row.chat_id for row in output.rows] == [chat_ids[-1]]


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
    assert isinstance(live_row.reentry, Blocked)
    assert isinstance(resolve_session_reentry(project_root.as_posix(), chat_id), Blocked)

    session_store.stop_session(runtime_root, chat_id)

    stopped_listing = session_list_sync(SessionListInput(project_root=project_root.as_posix()))
    stopped_row = next(row for row in stopped_listing.rows if row.chat_id == chat_id)
    assert isinstance(stopped_row.reentry, Blocked)
    assert isinstance(resolve_session_reentry(project_root.as_posix(), chat_id), Blocked)


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

    def fail_global_scan(*_args, **_kwargs):
        raise AssertionError("direct primary relationship should avoid list_spawns")

    monkeypatch.setattr(spawn_store, "list_spawns", fail_global_scan)
    try:
        listing = session_list_sync(SessionListInput(project_root=project_root.as_posix(), limit=1))
        assert isinstance(listing.rows[0].reentry, Blocked)
        assert isinstance(resolve_session_reentry(project_root.as_posix(), chat_id), Blocked)
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

    def fail_global_scan(*_args, **_kwargs):
        raise AssertionError("a recorded primary row should bound failed recovery")

    monkeypatch.setattr(spawn_store, "list_spawns", fail_global_scan)
    try:
        listing = session_list_sync(SessionListInput(project_root=project_root.as_posix(), limit=1))
        assert isinstance(listing.rows[0].reentry, Blocked)
        assert isinstance(resolve_session_reentry(project_root.as_posix(), chat_id), Blocked)
    finally:
        session_store.stop_session(runtime_root, chat_id)


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


def test_session_list_uses_index_for_missing_ids(
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
        output = session_list_sync(SessionListInput(project_root=project_root.as_posix()))
    finally:
        for chat_id in chat_ids:
            session_store.stop_session(runtime_root, chat_id)

    assert {row.chat_id for row in output.rows} == set(chat_ids)
    assert scan_count == 0


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
        output = session_list_sync(SessionListInput(project_root=project_root.as_posix(), limit=1))
    finally:
        session_store.stop_session(runtime_root, chat_ids[-1])

    assert output.total_count == 3
    assert [row.chat_id for row in output.rows] == [chat_ids[-1]]
    assert recovered_batches == [(chat_ids[-1],)]


def test_subset_search_is_ordered_and_failure_isolated(tmp_path: Path, monkeypatch) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", home.as_posix())
    matching_id = "55555555-5555-4555-8555-555555555555"
    other_id = "66666666-6666-4666-8666-666666666666"
    matching_chat = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=matching_id,
        native_store=(home / ".codex" / "sessions").as_posix(),
        model="gpt-5.4",
        kind="primary",
    )
    other_chat = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=other_id,
        native_store=(home / ".codex" / "sessions").as_posix(),
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
    assert record_scan_count == 0
    assert spawn_scan_count == 0


def test_subset_search_uses_recorded_primary_spawn_without_global_scan(
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
        native_store=(home / ".codex" / "sessions").as_posix(),
        model="gpt-5.4",
        kind="primary",
        spawn_id="p42",
    )
    session_store.stop_session(runtime_root, chat_id)
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id=chat_id,
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        kind="primary",
        prompt="primary",
        harness_session_id=harness_session_id,
    )
    _write_codex_rollout(
        home=home,
        project_root=project_root,
        session_id=harness_session_id,
        text="direct relationship needle",
    )

    def fail_global_scan(*_args, **_kwargs):
        raise AssertionError("direct primary relationship should avoid list_spawns")

    monkeypatch.setattr(spawn_store, "list_spawns", fail_global_scan)

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


def test_subset_search_does_not_borrow_sibling_history_when_primary_spawn_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root, runtime_root = _project_roots(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", home.as_posix())
    harness_session_id = "88888888-8888-4888-8888-888888888888"
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
                    "content": [{"type": "output_text", "text": "legacy history needle"}],
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
    assert steps[0].matched is False


def _bind_preview_native(root, key, events):
    store = root.parent / "native"
    store.mkdir(exist_ok=True)
    sid = "11111111-1111-4111-8111-111111111111"
    path = store / f"{sid}.jsonl"
    path.write_text(
        json.dumps({"sessionId": sid})
        + "\n"
        + "".join(json.dumps(event) + "\n" for event in events)
    )
    session_store.start_session(
        root,
        harness="claude",
        harness_session_id=sid,
        native_store=str(store),
        chat_id="c1",
        model="test",
    )
    session_store.stop_session(root, "c1")
    return path


def test_native_preview_stays_bounded_and_cached_after_reclaim(tmp_path, monkeypatch) -> None:
    from meridian.lib.ops import session_preview
    from meridian.lib.ops.session_archive import archive_history
    from meridian.lib.ops.session_preview import PreviewIdentity, SessionPreview
    from meridian.lib.state.history import ingest_portable_history
    from meridian.lib.state.history_index import HistoryIndex

    project, root = _project_roots(tmp_path)
    key = str(
        spawn_store.start_spawn(
            root, chat_id="c1", model="test", agent="coder", harness="codex", prompt="hello"
        )
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    ingest_portable_history(
        root, key, iter({"role": "assistant", "content": f"message {i}"} for i in range(100))
    )
    _bind_preview_native(
        root, key, ({"role": "assistant", "content": f"message {i}"} for i in range(100))
    )
    state = spawn_store.get_spawn(root, key)
    assert state is not None
    identity = PreviewIdentity(key, str(state.history_id))
    reader = SessionPreview(str(project))
    view = reader.refresh(identity, lambda: True)
    assert view is not None and view.status == "current · clipped"
    assert "message 99" in view.lines and "message 89" not in view.lines

    def forbid_body(*args, **kwargs):
        raise AssertionError("warm previews must not replay transcript bodies")

    with monkeypatch.context() as patch:
        patch.setattr(session_preview, "iter_source_events", forbid_body)
        assert reader.peek(identity) is not None
        assert reader.refresh(identity, lambda: True) == view
    result = archive_history(root, destination=tmp_path / "zips", refs=(key,), apply=True)
    assert result.reclaimed
    archived = reader.refresh(identity, lambda: True)
    assert archived is not None and "message 99" in archived.lines
    Path(result.archives[0]).rename(tmp_path / "offline.zip")
    offline = reader.refresh(identity, lambda: True)
    assert offline is not None and offline.state == "current"
    assert "message 99" in offline.lines
    HistoryIndex(root).rebuild()
    assert reader.peek(identity) is None


@pytest.mark.parametrize("unfinished", [False, True])
def test_preview_reparses_same_inode_rewrite_and_metadata_accepts_large_event(
    tmp_path, unfinished
) -> None:
    from meridian.lib.ops.session_preview import PreviewIdentity, SessionPreview
    from meridian.lib.state.history_index import HistoryIndex

    project, root = _project_roots(tmp_path)
    key = str(
        spawn_store.start_spawn(
            root, chat_id="c1", model="test", agent="coder", harness="codex", prompt="hello"
        )
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    path = _bind_preview_native(
        root,
        key,
        iter(
            (
                {"role": "assistant", "content": "PREFIX_A"},
                {"role": "assistant", "content": "x" * (2 * 1024 * 1024)},
            )
        ),
    )
    state = spawn_store.get_spawn(root, key)
    assert state is not None
    identity = PreviewIdentity(key, str(state.history_id))
    if unfinished:
        with path.open("ab") as handle:
            handle.write(b'{"unfinished":')
    reader = SessionPreview(str(project))
    before = reader.refresh(identity, lambda: True)
    assert before is not None and "PREFIX_A" in before.lines
    original = path.read_bytes()
    with path.open("r+b") as handle:
        handle.write(original.replace(b"PREFIX_A", b"PREFIX_B"))
    after = reader.refresh(identity, lambda: True)
    assert after is not None and "PREFIX_B" in after.lines and "PREFIX_A" not in after.lines
    # Rebuild reads metadata's last event, not every event or the preview body.
    HistoryIndex(root).rebuild()
    assert reader.peek(identity) is None
    rebuilt = reader.refresh(identity, lambda: True)
    assert rebuilt is not None and "PREFIX_B" in rebuilt.lines


@pytest.mark.parametrize("change", ["rebuild", "replace", "rewrite"])
def test_preview_rejects_changed_snapshot_at_publication(tmp_path, monkeypatch, change) -> None:
    from meridian.lib.ops.session_preview import PreviewIdentity, SessionPreview
    from meridian.lib.state.history_index import HistoryIndex

    project, root = _project_roots(tmp_path)
    key = str(
        spawn_store.start_spawn(
            root, chat_id="c1", model="test", agent="coder", harness="codex", prompt="hello"
        )
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    path = _bind_preview_native(
        root,
        key,
        iter(
            (
                {"role": "assistant", "content": "snapshot A"},
                {"role": "assistant", "content": "z" * 500},
            )
        ),
    )
    state = spawn_store.get_spawn(root, key)
    assert state is not None
    identity = PreviewIdentity(key, str(state.history_id))
    reader = SessionPreview(str(project))
    original = HistoryIndex.store_preview

    def change_before_publication(index, *args, **kwargs):
        if change == "rebuild":
            index.rebuild()
        else:
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes().replace(b"snapshot A", b"snapshot B"))
            if change == "rewrite":
                with path.open("r+b") as handle:
                    handle.write(replacement.read_bytes())
            else:
                replacement.replace(path)
        return original(index, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(HistoryIndex, "store_preview", change_before_publication)
        stale = reader.refresh(identity, lambda: True)
    assert stale is not None and stale.state == "updating"
    assert "snapshot A" not in stale.lines
    assert reader.peek(identity) is None
    fresh = reader.refresh(identity, lambda: True)
    assert fresh is not None and fresh.state == "current"
    assert ("snapshot B" if change != "rebuild" else "snapshot A") in fresh.lines


def test_native_preview_does_not_follow_reused_chat_generation(tmp_path, monkeypatch) -> None:
    from meridian.lib.ops.session_preview import PreviewIdentity, SessionPreview

    project, root = _project_roots(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    for number in (1, 2):
        native = "00000001-1111-4111-8111-111111111111"
        _write_codex_rollout(
            home=home, project_root=project, session_id=native, text=f"generation {number}"
        )
        session_store.start_session(
            root,
            harness="codex",
            harness_session_id=native,
            model="test",
            chat_id="c1",
            kind="primary",
        )
        session_store.stop_session(root, "c1")
        if number == 1:
            row = session_list_sync(SessionListInput(project_root=str(project))).rows[0]
            old = PreviewIdentity(row.chat_id, row.history_id, row.session_generation)
    reader = SessionPreview(str(project))
    view = reader.refresh(old, lambda: True)
    assert view is not None and view.state == "unavailable"
    assert "generation 2" not in view.lines
    assert reader.peek(old) is None
