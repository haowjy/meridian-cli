"""Post-stop native capture is bound to the completed aggregate, not latest chat."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.harness.pi_paths import resolve_pi_spawn_session_root
from meridian.lib.ops.session_archive import materialize_native_history, session_stop_maintenance
from meridian.lib.ops.session_target import resolve_transcript_source
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.native_snapshot import (
    NATIVE_SNAPSHOT_FILENAME,
    TranscriptValidation,
    read_snapshot,
)
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.primary_meta import PrimaryMetadata, write_primary_metadata


def _snapshot_path(root: Path, key: str) -> Path:
    return root / "spawns" / key / NATIVE_SNAPSHOT_FILENAME


def _assert_sealed_snapshot(path: Path, *, contains: str, excludes: str | None = None) -> None:
    assert path.is_file()
    text = path.read_text()
    assert contains in text
    if excludes is not None:
        assert excludes not in text
    validation = TranscriptValidation()
    with path.open("rb") as handle:
        list(read_snapshot(handle, validation=validation))
    assert validation.state == "complete"
    assert validation.descriptor is not None
    assert validation.header is not None


def _assert_not_captured(root: Path, key: str) -> None:
    assert not _snapshot_path(root, key).exists()
    assert not (root / "spawns" / key / "history.jsonl").exists()


def test_stop_maintenance_captures_completed_spawn_with_another_chat(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    native_root = resolve_pi_spawn_session_root()
    native_root.mkdir(parents=True)
    keys: list[str] = []
    for chat_id, native_id in (("c1", "old-native"), ("c2", "new-native")):
        key = spawn_store.start_spawn(
            root,
            chat_id=chat_id,
            prompt="question",
            harness="pi",
            model="test",
            agent="coder",
            kind="primary",
            harness_session_id=native_id,
        )
        keys.append(key)
        session_store.start_session(
            root,
            "pi",
            native_id,
            "test",
            chat_id=chat_id,
            kind="primary",
            spawn_id=key,
            native_store=str(native_root),
        )
        session_store.stop_session(root, chat_id)
        spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
        events = [
            {"type": "session", "version": 3, "id": native_id, "cwd": str(project)},
            {
                "type": "message",
                "id": "a",
                "parentId": None,
                "message": {"role": "assistant", "content": native_id},
            },
        ]
        (native_root / f"timestamp_{native_id}.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
    latest = session_store.get_session_record(root, "c2")
    assert latest is not None and latest.spawn_id == keys[1]
    assert session_stop_maintenance(project, keys[0]) is None
    captured = _snapshot_path(root, keys[0])
    _assert_sealed_snapshot(captured, contains="old-native", excludes="new-native")
    _assert_not_captured(root, keys[1])
    before = captured.read_bytes()
    assert session_stop_maintenance(project, keys[0]) is None
    assert captured.read_bytes() == before
    (native_root / "timestamp_old-native.jsonl").unlink()
    assert session_stop_maintenance(project, keys[0]) is None
    assert captured.read_bytes() == before
    assert session_stop_maintenance(project, "p999999") is not None
    assert captured.read_bytes() == before
    _assert_not_captured(root, keys[1])


def _capture_fixture(tmp_path: Path, monkeypatch, *, native_id: str | None = "exact-native"):
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    key = spawn_store.start_spawn(
        root,
        chat_id="c1",
        prompt="question",
        harness="pi",
        model="test",
        agent="coder",
        kind="primary",
        harness_session_id=native_id,
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    native_root = resolve_pi_spawn_session_root()
    native_root.mkdir(parents=True)
    if native_id is not None:
        session_store.start_session(
            root,
            "pi",
            native_id,
            "test",
            chat_id="c1",
            kind="primary",
            spawn_id=key,
            native_store=str(native_root),
        )
        session_store.stop_session(root, "c1")
    native = native_root / "timestamp_exact-native.jsonl"
    native.write_text(json.dumps({"type": "session", "version": 3, "id": "exact-native"}) + "\n")
    return project, root, key, native


def test_capture_does_not_discover_an_unrecorded_native_session(tmp_path: Path, monkeypatch):
    project, root, key, native = _capture_fixture(tmp_path, monkeypatch, native_id=None)
    with pytest.raises(ValueError, match="unbound"):
        materialize_native_history(project, root, key)
    assert native.exists()
    _assert_not_captured(root, key)


def test_capture_missing_exact_source_never_uses_newer_detection(tmp_path: Path, monkeypatch):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch, native_id="missing-native")
    with pytest.raises(ValueError, match="native_transcript_missing"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)


def test_capture_resolution_bypasses_owned_stream_and_disposable_index(tmp_path: Path, monkeypatch):
    project, root, key, native = _capture_fixture(tmp_path, monkeypatch)
    stream = root / "spawns" / key / "history.jsonl"
    stream.write_bytes(b'partial original stream\n{"torn":')
    before = stream.stat()
    target = resolve_transcript_source(
        ref=key,
        file_path=None,
        project_root=project,
        runtime_root=root,
        purpose="capture",
    )
    assert target.source.kind == "native_file" and target.source.path == native
    assert target.source.session_id == "exact-native"
    assert stream.stat() == before
    assert not (root / "history-index" / "history.sqlite3").exists()


def test_capture_uses_bound_generation_not_sidecar_identity(tmp_path: Path, monkeypatch):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    write_primary_metadata(
        root / "spawns" / key,
        PrimaryMetadata(harness_session_id="other-native"),
        runtime_root=root,
        spawn_id=key,
    )
    target = resolve_transcript_source(
        ref=key, project_root=project, runtime_root=root, purpose="capture"
    )
    assert target.source.session_id == "exact-native"


def test_capture_rejects_active_same_native_owner_then_retries(tmp_path: Path, monkeypatch):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    owner = spawn_store.start_spawn(
        root,
        chat_id="c2",
        harness="pi",
        harness_session_id="exact-native",
        kind="primary",
        prompt="continued",
        model="test",
        agent="coder",
    )
    with pytest.raises(ValueError, match="active native owner"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)
    spawn_store.finalize_spawn(root, owner, status="succeeded", exit_code=0, origin="runner")
    materialize_native_history(project, root, key)
    _assert_sealed_snapshot(_snapshot_path(root, key), contains="exact-native")
    _assert_not_captured(root, owner)


def test_capture_rejects_same_native_session_lease_without_spawn(tmp_path: Path, monkeypatch):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    session_store.start_session(root, "pi", "exact-native", "test", chat_id="c2")
    try:
        with pytest.raises(ValueError, match="active native owner"):
            materialize_native_history(project, root, key)
        _assert_not_captured(root, key)
    finally:
        session_store.stop_session(root, "c2")
    materialize_native_history(project, root, key)
    _assert_sealed_snapshot(_snapshot_path(root, key), contains="exact-native")


def test_capture_exact_generation_supplies_identity_not_newer_chat(tmp_path: Path, monkeypatch):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch, native_id=None)
    session_store.start_session(
        root,
        "pi",
        "exact-native",
        "test",
        chat_id="c1",
        kind="primary",
        spawn_id=key,
        native_store=str(resolve_pi_spawn_session_root()),
    )
    session_store.stop_session(root, "c1")
    session_store.start_session(root, "pi", "", "test", chat_id="c1", kind="primary")
    session_store.stop_session(root, "c1")
    try:
        materialize_native_history(project, root, key)
        _assert_sealed_snapshot(
            _snapshot_path(root, key), contains="exact-native", excludes="new-native"
        )
    finally:
        session_store.stop_session(root, "c1")


def test_capture_rejects_unreleased_live_scope_on_terminal_owner(tmp_path: Path, monkeypatch):
    import os

    import psutil

    from meridian.lib.core.types import SpawnId
    from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
    from meridian.lib.state.process_scope_projection import mark_scope_released, record_scope

    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    owner = spawn_store.start_spawn(
        root,
        chat_id="c2",
        harness="pi",
        harness_session_id=None,
        kind="primary",
        prompt="continued",
        model="test",
        agent="coder",
    )
    # Its native binding is only in its exact session generation, not state.json.
    session_store.start_session(
        root,
        "pi",
        "exact-native",
        "test",
        chat_id="c2",
        kind="primary",
        spawn_id=owner,
    )
    session_store.stop_session(root, "c2")
    scope = ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="session_owned",
        owner_id="exact-native",
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=os.getpid(),
        root_created_at_epoch=psutil.Process().create_time(),
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )
    record_scope(root, SpawnId(owner), scope)
    spawn_store.finalize_spawn(root, owner, status="succeeded", exit_code=0, origin="runner")
    with pytest.raises(ValueError, match="active native owner"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)
    mark_scope_released(root, SpawnId(owner), scope.release_id)
    materialize_native_history(project, root, key)
    _assert_sealed_snapshot(_snapshot_path(root, key), contains="exact-native")


def test_capture_rechecks_owner_after_native_read(tmp_path: Path, monkeypatch):
    from meridian.lib.harness import transcript_capture

    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    original = transcript_capture.native_capture

    def capture_with_new_owner(**kwargs):
        observation = original(**kwargs)
        records = observation.records

        def records_with_owner():
            yield from records()
            spawn_store.start_spawn(
                root,
                chat_id="c2",
                harness="pi",
                harness_session_id="exact-native",
                kind="primary",
                prompt="continued",
                model="test",
                agent="coder",
            )

        observation.records = records_with_owner
        return observation

    monkeypatch.setattr(transcript_capture, "native_capture", capture_with_new_owner)
    with pytest.raises(ValueError, match="active native owner"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)


@pytest.mark.parametrize("binding", ["state", "sidecar"])
@pytest.mark.parametrize("owner_harness", ["pi", "   ", ""])
def test_capture_joins_native_identity_to_exact_linked_live_lease(
    tmp_path: Path, monkeypatch, binding, owner_harness
):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    owner = spawn_store.start_spawn(
        root,
        chat_id="c2",
        harness=owner_harness,
        kind="primary",
        prompt="continued",
        model="test",
        agent="coder",
        harness_session_id="exact-native" if binding == "state" else None,
    )
    session_store.start_session(
        root, "pi", "", "test", chat_id="c2", kind="primary", spawn_id=owner
    )
    if binding == "sidecar":
        write_primary_metadata(
            root / "spawns" / owner,
            PrimaryMetadata(harness_session_id="exact-native"),
            runtime_root=root,
            spawn_id=owner,
        )
    spawn_store.finalize_spawn(root, owner, status="succeeded", exit_code=0, origin="runner")
    try:
        assert session_store.is_session_lease_owner_alive(root, "c2")
        with pytest.raises(ValueError, match="active native owner"):
            materialize_native_history(project, root, key)
        _assert_not_captured(root, key)
    finally:
        session_store.stop_session(root, "c2")
    # A new generation using the same c2 alias must not lend its lease to the
    # stopped generation whose state/sidecar supplied the matching native ID.
    session_store.start_session(root, "pi", "different-native", "test", chat_id="c2")
    try:
        materialize_native_history(project, root, key)
        _assert_sealed_snapshot(_snapshot_path(root, key), contains="exact-native")
    finally:
        session_store.stop_session(root, "c2")


def test_child_archive_uses_native_snapshot_not_runner_history(tmp_path: Path, monkeypatch):
    from meridian.lib.ops.session_archive import archive_history
    from meridian.lib.state.retention_archive import iter_archived_events

    project, root, _, _ = _capture_fixture(tmp_path, monkeypatch)
    native_root = resolve_pi_spawn_session_root()
    child_native_id = "child-native"
    child = native_root / f"timestamp_{child_native_id}.jsonl"
    child.write_text(
        json.dumps({"type": "session", "version": 3, "id": child_native_id})
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "native-child-turn",
                "parentId": None,
                "message": {"role": "assistant", "content": "native child answer"},
            }
        )
        + "\n"
    )
    key = spawn_store.start_spawn(
        root,
        chat_id="c2",
        harness="pi",
        harness_session_id=child_native_id,
        kind="child",
        prompt="child",
        model="test",
        agent="coder",
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    session_store.start_session(
        root,
        "pi",
        child_native_id,
        "test",
        chat_id="c2",
        kind="spawn",
        spawn_id=key,
        native_store=str(native_root),
    )
    session_store.stop_session(root, "c2")
    state = spawn_store.get_spawn(root, key)
    assert state is not None and state.history_id is not None
    stream = root / "spawns" / key / "history.jsonl"
    stream.write_text(json.dumps({"type": "runner-only"}) + "\n")
    materialize_native_history(project, root, key)
    _assert_sealed_snapshot(_snapshot_path(root, key), contains="native child answer")
    result = archive_history(
        root,
        destination=tmp_path / "archives",
        refs=(key,),
        apply=True,
        project_root=project,
    )
    assert not result.errors
    assert result.reclaimed == (str(state.history_id),)
    retained = list(iter_archived_events(Path(result.archives[0]), state.history_id))
    assert any(event.get("id") == "native-child-turn" for event in retained)
    assert all(event.get("type") != "runner-only" for event in retained)


def test_archive_apply_captures_headless_spawn_and_preserves_native_log(
    tmp_path: Path, monkeypatch
):
    import zipfile

    from meridian.lib.ops.session_archive import archive_history
    from meridian.lib.ops.session_log import SessionLogInput, session_log_sync

    project, root, _, _ = _capture_fixture(tmp_path, monkeypatch)
    native_root = resolve_pi_spawn_session_root()
    native_id = "headless-native"
    native = native_root / f"timestamp_{native_id}.jsonl"
    native.write_text(
        json.dumps({"type": "session", "version": 3, "id": native_id})
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "headless-answer",
                "parentId": None,
                "message": {"role": "assistant", "content": "native answer"},
            }
        )
        + "\n"
    )
    key = spawn_store.start_spawn(
        root,
        chat_id="c2",
        harness="pi",
        harness_session_id=native_id,
        kind="child",
        prompt="headless prompt",
        model="test",
        agent="coder",
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    session_store.start_session(
        root,
        "pi",
        native_id,
        "test",
        chat_id="c2",
        kind="spawn",
        spawn_id=key,
        native_store=str(native_root),
    )
    session_store.stop_session(root, "c2")
    (root / "spawns" / key / "history.jsonl").write_text(
        json.dumps({"type": "runner-only"}) + "\n"
    )

    dry_run = archive_history(
        root,
        destination=tmp_path / "archives",
        refs=(key,),
        project_root=project,
    )
    assert dry_run.preparation_required == (key,)
    assert f"Apply will capture native snapshot: {key}" in dry_run.format_text()
    assert not _snapshot_path(root, key).exists()

    result = archive_history(
        root,
        destination=tmp_path / "archives",
        refs=(key,),
        apply=True,
        project_root=project,
    )

    assert not result.errors
    assert result.archives
    with zipfile.ZipFile(result.archives[0]) as archive:
        names = archive.namelist()
        assert any(name.endswith("native-transcript.jsonl") for name in names)
        assert not any(name.endswith("history.jsonl") for name in names)
        assert not any(name.endswith("last-observed-event.json") for name in names)
    output = session_log_sync(SessionLogInput(ref=key, project_root=str(project), full=True))
    assert "native answer" in output.format_text()
    assert "runner-only" not in output.format_text()


def test_archive_refuses_legacy_runner_history_as_transcript(tmp_path: Path, monkeypatch):
    from meridian.lib.ops.session_archive import archive_history
    from meridian.lib.state.retention_archive import (
        capture_record,
        iter_archived_events,
        publish_archive,
    )
    from meridian.lib.state.retention_restore import restore_archive

    _, root, _, _ = _capture_fixture(tmp_path, monkeypatch)
    legacy_key = spawn_store.start_spawn(
        root,
        chat_id=None,
        harness="pi",
        kind="child",
        prompt="child",
        model="test",
        agent="coder",
    )
    spawn_store.finalize_spawn(root, legacy_key, status="succeeded", exit_code=0, origin="runner")
    legacy_state = spawn_store.get_spawn(root, legacy_key)
    assert legacy_state is not None and legacy_state.history_id is not None
    events = [
        {"type": "message", "message": {"role": "assistant", "content": "first attempt"}},
        {"event_type": "legacy.runner.boundary", "attempt": 1},
        {"type": "message", "message": {"role": "assistant", "content": "retry answer"}},
    ]
    legacy = root / "spawns" / legacy_key / "history.jsonl"
    legacy.write_text("".join(json.dumps(event) + "\n" for event in events))
    result = archive_history(
        root,
        destination=tmp_path / "archives",
        refs=(legacy_key,),
        apply=True,
    )
    assert not result.reclaimed
    assert not result.archives
    assert any("no exact native source is bound" in error for error in result.errors)
    legacy_record = capture_record(
        root / "spawns" / legacy_key,
        legacy_state,
        None,
        "2025-01-01T00:00:00+00:00",
    )
    old_archive = publish_archive(
        root,
        tmp_path / "old-archive",
        (legacy_record,),
    )
    archive_path = Path(old_archive.destination) / old_archive.zip_name
    restored_ids = restore_archive(
        tmp_path / "restored-runtime", archive_path, (str(legacy_state.history_id),)
    )
    restored_history = tmp_path / "restored-runtime" / "spawns" / restored_ids[0] / "history.jsonl"
    from meridian.lib.state.retention_archive import inventory

    # Byte inventory is the only supported interpretation of legacy members.
    original_member = next(m for m in inventory(legacy.parent) if m.name == "history.jsonl")
    restored_member = next(
        m for m in inventory(restored_history.parent) if m.name == "history.jsonl"
    )
    assert restored_member == original_member
    with pytest.raises(ValueError, match="members are inert"):
        list(iter_archived_events(archive_path, legacy_state.history_id))


@pytest.mark.parametrize("owner_harness", ["pi", " PI "])
def test_capture_owner_harness_matching_uses_resolver_normalization(
    tmp_path: Path, monkeypatch, owner_harness
):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    spawn_store.start_spawn(
        root,
        chat_id="c2",
        harness=owner_harness,
        kind="primary",
        prompt="continued",
        model="test",
        agent="coder",
        harness_session_id="exact-native",
    )
    with pytest.raises(ValueError, match="active native owner"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)


@pytest.mark.parametrize("linked_harness", ["pi", "codex"])
def test_capture_conflicting_owner_facts_are_conservative_without_cross_harness_aliasing(
    tmp_path: Path, monkeypatch, linked_harness
):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    owner = spawn_store.start_spawn(
        root,
        chat_id="c2",
        harness="codex",
        kind="primary",
        prompt="continued",
        model="test",
        agent="coder",
        harness_session_id="exact-native",
    )
    session_store.start_session(
        root,
        linked_harness,
        "",
        "test",
        chat_id="c2",
        kind="primary",
        spawn_id=owner,
    )
    spawn_store.finalize_spawn(root, owner, status="succeeded", exit_code=0, origin="runner")
    try:
        if linked_harness == "pi":
            # A conflicting owner's known facts must not hide a possible live
            # writer. It is not eligible for capture itself, either.
            with pytest.raises(ValueError, match="active native owner"):
                materialize_native_history(project, root, key)
            _assert_not_captured(root, key)
        else:
            # Equal opaque session IDs in distinct harness namespaces do not match.
            materialize_native_history(project, root, key)
            _assert_sealed_snapshot(_snapshot_path(root, key), contains="exact-native")
    finally:
        session_store.stop_session(root, "c2")


def test_history_jsonl_existence_is_not_capture_complete(tmp_path: Path, monkeypatch):
    project, root, key, _native = _capture_fixture(tmp_path, monkeypatch)
    stream = root / "spawns" / key / "history.jsonl"
    stream.write_bytes(b'{"partial":true}\n')
    before = stream.stat()
    materialize_native_history(project, root, key)
    _assert_sealed_snapshot(_snapshot_path(root, key), contains="exact-native")
    assert stream.stat() == before


def test_known_incomplete_pi_tail_does_not_publish(tmp_path: Path, monkeypatch):
    project, root, key, native = _capture_fixture(tmp_path, monkeypatch)
    native.write_text(
        json.dumps({"type": "session", "version": 3, "id": "exact-native"})
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "a",
                "parentId": None,
                "message": {
                    "role": "assistant",
                    "content": "cut short",
                    "stopReason": "aborted",
                },
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="incomplete"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)


@pytest.mark.parametrize("stop_reason", ["cancel", "cancelled", "canceled"])
def test_known_incomplete_pi_cancelled_tail_does_not_publish(
    tmp_path: Path, monkeypatch, stop_reason: str
):
    project, root, key, native = _capture_fixture(tmp_path, monkeypatch)
    native.write_text(
        json.dumps({"type": "session", "version": 3, "id": "exact-native"})
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "a",
                "parentId": None,
                "message": {
                    "role": "assistant",
                    "content": "user cancelled",
                    "stopReason": stop_reason,
                },
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="incomplete"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)


def test_known_incomplete_pi_truncated_tail_does_not_publish(tmp_path: Path, monkeypatch):
    project, root, key, native = _capture_fixture(tmp_path, monkeypatch)
    native.write_text(
        json.dumps({"type": "session", "version": 3, "id": "exact-native"})
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "a",
                "parentId": None,
                "message": {
                    "role": "assistant",
                    "content": "truncated answer",
                    "stopReason": "length",
                },
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="incomplete"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)


def test_pi_normal_stop_publishes(tmp_path: Path, monkeypatch):
    project, root, key, native = _capture_fixture(tmp_path, monkeypatch)
    native.write_text(
        json.dumps({"type": "session", "version": 3, "id": "exact-native"})
        + "\n"
        + json.dumps(
            {
                "type": "message",
                "id": "a",
                "parentId": None,
                "message": {
                    "role": "assistant",
                    "content": "finished answer",
                    "stopReason": "stop",
                },
            }
        )
        + "\n"
    )
    materialize_native_history(project, root, key)
    _assert_sealed_snapshot(_snapshot_path(root, key), contains="finished answer")


def test_capture_retry_removes_stale_atomic_temps(tmp_path: Path, monkeypatch):
    project, root, key, _native = _capture_fixture(tmp_path, monkeypatch)
    stale_snapshot = root / "spawns" / key / ".native-transcript.jsonl.deadbeef.tmp"
    stale_snapshot.write_text("partial snapshot")
    materialize_native_history(project, root, key)
    _assert_sealed_snapshot(_snapshot_path(root, key), contains="exact-native")
    assert not stale_snapshot.exists()


def _native_file_capture(tmp_path: Path, harness: str, events: list[dict[str, object]]):
    from meridian.lib.harness.transcript_capture import native_capture

    path = tmp_path / f"{harness}.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return native_capture(kind="native_file", harness=harness, session_id="s", path=path)


def test_codex_complete_tail_qualifies(tmp_path: Path):
    capture = _native_file_capture(
        tmp_path,
        "codex",
        [
            {"type": "session_meta", "payload": {"id": "s"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
            {"type": "task_started"},
            {"type": "task_complete"},
        ],
    )
    list(capture.records())
    assert capture.finish() is not None


def test_codex_open_task_tail_is_known_incomplete(tmp_path: Path):
    capture = _native_file_capture(
        tmp_path,
        "codex",
        [
            {"type": "session_meta", "payload": {"id": "s"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
            {"type": "task_started"},
        ],
    )
    list(capture.records())
    with pytest.raises(ValueError, match="incomplete"):
        capture.finish()


def test_codex_aborted_tail_is_known_incomplete(tmp_path: Path):
    capture = _native_file_capture(
        tmp_path,
        "codex",
        [
            {"type": "session_meta", "payload": {"id": "s"}},
            {"type": "turn_aborted"},
        ],
    )
    list(capture.records())
    with pytest.raises(ValueError, match="incomplete"):
        capture.finish()


def test_claude_complete_tail_qualifies(tmp_path: Path):
    capture = _native_file_capture(
        tmp_path,
        "claude",
        [{"type": "assistant", "message": {"role": "assistant", "content": []}}],
    )
    list(capture.records())
    assert capture.finish() is not None


def test_claude_error_tail_is_known_incomplete(tmp_path: Path):
    capture = _native_file_capture(
        tmp_path,
        "claude",
        [{"type": "result", "is_error": True}],
    )
    list(capture.records())
    with pytest.raises(ValueError, match="incomplete"):
        capture.finish()


def test_claude_error_resolved_by_later_message_qualifies(tmp_path: Path):
    capture = _native_file_capture(
        tmp_path,
        "claude",
        [
            {"type": "result", "is_error": True},
            {"type": "assistant", "message": {"role": "assistant", "content": []}},
        ],
    )
    list(capture.records())
    assert capture.finish() is not None


def test_corrupt_published_snapshot_is_not_overwritten(tmp_path: Path, monkeypatch):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    captured = _snapshot_path(root, key)
    captured.write_text("{not a snapshot\n")
    before = captured.read_bytes()
    with pytest.raises(ValueError, match="corrupt"):
        materialize_native_history(project, root, key)
    assert captured.read_bytes() == before


def _opencode_capture_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    messages: list[tuple[str, dict[str, object], list[dict[str, object]]]],
):
    from tests.support.opencode_db import write_opencode_db_session_with_parts

    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    opencode_home = tmp_path / "opencode"
    session_id = "ses_opencode_capture"
    write_opencode_db_session_with_parts(
        db_path=opencode_home / "opencode.db",
        session_id=session_id,
        messages=messages,
    )
    monkeypatch.setenv("OPENCODE_HOME", str(opencode_home))
    storage_root = opencode_home / "storage"
    session_file = storage_root / "session" / f"{session_id}.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("{}", encoding="utf-8")
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    session_store.start_session(
        root,
        harness="opencode",
        harness_session_id=session_id,
        native_store=(storage_root.parent / "opencode.db").as_posix(),
        model="test",
        chat_id="c1",
        kind="primary",
    )
    key = spawn_store.start_spawn(
        root,
        chat_id="c1",
        prompt="question",
        harness="opencode",
        model="test",
        agent="coder",
        kind="primary",
        harness_session_id=session_id,
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    session_store.stop_session(root, "c1")
    return project, root, key


def test_known_incomplete_opencode_response_does_not_publish(tmp_path: Path, monkeypatch):
    project, root, key = _opencode_capture_fixture(
        tmp_path,
        monkeypatch,
        messages=[
            (
                "assistant",
                {"time": {"created": 1}},
                [{"type": "text", "text": "partial answer..."}],
            )
        ],
    )
    with pytest.raises(ValueError, match="incomplete"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)


def test_completed_opencode_response_publishes(tmp_path: Path, monkeypatch):
    project, root, key = _opencode_capture_fixture(
        tmp_path,
        monkeypatch,
        messages=[
            (
                "assistant",
                {"time": {"created": 1, "completed": 2}},
                [{"type": "text", "text": "final answer"}],
            )
        ],
    )
    materialize_native_history(project, root, key)
    _assert_sealed_snapshot(_snapshot_path(root, key), contains="final answer")


def _opencode_v2_capture_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    messages: list[tuple[str, dict[str, object]]],
    idle_outcome: str | None = None,
):
    from tests.support.opencode_db import write_opencode_v2_db_session

    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    opencode_home = tmp_path / "opencode"
    session_id = "ses_opencode_capture_v2"
    write_opencode_v2_db_session(
        db_path=opencode_home / "opencode.db",
        session_id=session_id,
        messages=messages,
        idle_outcome=idle_outcome,
    )
    monkeypatch.setenv("OPENCODE_HOME", str(opencode_home))
    storage_root = opencode_home / "storage"
    session_file = storage_root / "session" / f"{session_id}.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("{}", encoding="utf-8")
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    session_store.start_session(
        root,
        harness="opencode",
        harness_session_id=session_id,
        native_store=(storage_root.parent / "opencode.db").as_posix(),
        model="test",
        chat_id="c1",
        kind="primary",
    )
    key = spawn_store.start_spawn(
        root,
        chat_id="c1",
        prompt="question",
        harness="opencode",
        model="test",
        agent="coder",
        kind="primary",
        harness_session_id=session_id,
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    session_store.stop_session(root, "c1")
    return project, root, key


def test_v2_opencode_session_publishes_with_v2_dialect(tmp_path: Path, monkeypatch):
    project, root, key = _opencode_v2_capture_fixture(
        tmp_path,
        monkeypatch,
        idle_outcome="succeeded",
        messages=[
            ("user", {"time": {"created": 1}, "text": "hi"}),
            (
                "assistant",
                {
                    "time": {"created": 2},
                    "content": [{"type": "text", "text": "final answer"}],
                },
            ),
            ("idle", {"time": {"created": 3}, "outcome": "succeeded"}),
        ],
    )
    materialize_native_history(project, root, key)
    snapshot = _snapshot_path(root, key)
    _assert_sealed_snapshot(snapshot, contains="final answer")
    validation = TranscriptValidation()
    with snapshot.open("rb") as handle:
        list(read_snapshot(handle, validation=validation))
    assert validation.header is not None
    assert validation.header.dialect == "opencode.transcript.v2"
    assert "opencode.transcript.v2" in snapshot.read_text()


def test_v2_opencode_pending_tool_tail_does_not_publish(tmp_path: Path, monkeypatch):
    project, root, key = _opencode_v2_capture_fixture(
        tmp_path,
        monkeypatch,
        messages=[
            ("user", {"time": {"created": 1}, "text": "hi"}),
            (
                "assistant",
                {
                    "time": {"created": 2},
                    "content": [
                        {"type": "text", "text": "partial answer..."},
                        {
                            "type": "tool",
                            "name": "shell",
                            "state": {"status": "running", "input": {"command": "sleep 1"}},
                        },
                    ],
                },
            ),
        ],
    )
    with pytest.raises(ValueError, match="incomplete"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)


def test_v2_opencode_missing_completion_outcome_does_not_publish(tmp_path: Path, monkeypatch):
    project, root, key = _opencode_v2_capture_fixture(
        tmp_path,
        monkeypatch,
        messages=[
            ("user", {"time": {"created": 1}, "text": "hi"}),
            (
                "assistant",
                {
                    "time": {"created": 2},
                    "content": [{"type": "text", "text": "partial answer..."}],
                },
            ),
        ],
    )
    with pytest.raises(ValueError, match="incomplete"):
        materialize_native_history(project, root, key)
    _assert_not_captured(root, key)
