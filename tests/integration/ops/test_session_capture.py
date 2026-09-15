"""Post-stop native capture is bound to the completed aggregate, not latest chat."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.harness.pi import PiAdapter
from meridian.lib.harness.pi_paths import resolve_pi_spawn_session_root
from meridian.lib.ops.session_archive import materialize_native_history, session_stop_maintenance
from meridian.lib.ops.session_target import resolve_session_log_target
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.primary_meta import PrimaryMetadata, write_primary_metadata


def test_stop_maintenance_captures_completed_spawn_after_chat_reuse(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    root = resolve_project_runtime_root_for_write(project)
    native_root = resolve_pi_spawn_session_root()
    native_root.mkdir(parents=True)
    keys: list[str] = []
    for native_id in ("old-native", "new-native"):
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
        keys.append(key)
        session_store.start_session(
            root,
            "pi",
            native_id,
            "test",
            chat_id="c1",
            kind="primary",
            spawn_id=key,
        )
        session_store.stop_session(root, "c1")
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
    latest = session_store.get_session_record(root, "c1")
    assert latest is not None and latest.spawn_id == keys[1]
    assert session_stop_maintenance(project, keys[0]) is None
    captured = root / "spawns" / keys[0] / "history.jsonl"
    assert captured.exists()
    assert "old-native" in captured.read_text() and "new-native" not in captured.read_text()
    assert not (root / "spawns" / keys[1] / "history.jsonl").exists()
    before = captured.read_bytes()
    assert session_stop_maintenance(project, "p999999") is not None
    assert captured.read_bytes() == before
    assert not (root / "spawns" / keys[1] / "history.jsonl").exists()


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
    native = native_root / "timestamp_exact-native.jsonl"
    native.write_text(json.dumps({"type": "session", "version": 3, "id": "exact-native"}) + "\n")
    return project, root, key, native


def test_capture_does_not_discover_an_unrecorded_native_session(tmp_path: Path, monkeypatch):
    project, root, key, native = _capture_fixture(tmp_path, monkeypatch, native_id=None)
    monkeypatch.setattr(PiAdapter, "detect_primary_session_id", lambda *a, **kw: "exact-native")
    with pytest.raises(ValueError, match="exact native identity"):
        materialize_native_history(project, root, key)
    assert native.exists()
    assert not (root / "spawns" / key / "history.jsonl").exists()


def test_capture_missing_exact_source_never_uses_newer_detection(tmp_path: Path, monkeypatch):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch, native_id="missing-native")
    monkeypatch.setattr(PiAdapter, "detect_primary_session_id", lambda *a, **kw: "exact-native")
    with pytest.raises(FileNotFoundError, match="missing-native"):
        materialize_native_history(project, root, key)
    assert not (root / "spawns" / key / "history.jsonl").exists()


def test_capture_resolution_bypasses_owned_stream_and_disposable_index(tmp_path: Path, monkeypatch):
    project, root, key, native = _capture_fixture(tmp_path, monkeypatch)
    stream = root / "spawns" / key / "history.jsonl"
    stream.write_bytes(b'partial original stream\n{"torn":')
    before = stream.read_bytes()
    target = resolve_session_log_target(
        ref=key,
        file_path=None,
        project_root=project,
        runtime_root=root,
        purpose="capture",
    )
    assert len(target.sources) == 1
    assert target.sources[0].kind == "file" and target.file_path == native
    assert target.session_id == "exact-native"
    assert stream.read_bytes() == before
    assert not (root / "history-index" / "history.sqlite3").exists()


def test_capture_conflicting_sidecar_identity_is_not_a_precedence_choice(
    tmp_path: Path, monkeypatch
):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    write_primary_metadata(
        root / "spawns" / key,
        PrimaryMetadata(harness_session_id="other-native"),
        runtime_root=root,
        spawn_id=key,
    )
    with pytest.raises(ValueError, match="Conflicting native identity"):
        materialize_native_history(project, root, key)
    assert not (root / "spawns" / key / "history.jsonl").exists()


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
    assert not (root / "spawns" / key / "history.jsonl").exists()
    spawn_store.finalize_spawn(root, owner, status="succeeded", exit_code=0, origin="runner")
    materialize_native_history(project, root, key)
    assert (root / "spawns" / key / "history.jsonl").exists()
    assert not (root / "spawns" / owner / "history.jsonl").exists()


def test_capture_rejects_same_native_session_lease_without_spawn(tmp_path: Path, monkeypatch):
    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    session_store.start_session(root, "pi", "exact-native", "test", chat_id="c2")
    try:
        with pytest.raises(ValueError, match="active native owner"):
            materialize_native_history(project, root, key)
        assert not (root / "spawns" / key / "history.jsonl").exists()
    finally:
        session_store.stop_session(root, "c2")
    materialize_native_history(project, root, key)
    assert (root / "spawns" / key / "history.jsonl").exists()


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
    )
    session_store.stop_session(root, "c1")
    session_store.start_session(root, "pi", "new-native", "test", chat_id="c1", kind="primary")
    try:
        materialize_native_history(project, root, key)
        captured = root / "spawns" / key / "history.jsonl"
        assert "exact-native" in captured.read_text() and "new-native" not in captured.read_text()
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
    assert not (root / "spawns" / key / "history.jsonl").exists()
    mark_scope_released(root, SpawnId(owner), scope.release_id)
    materialize_native_history(project, root, key)
    assert (root / "spawns" / key / "history.jsonl").exists()


def test_capture_rechecks_owner_after_native_read(tmp_path: Path, monkeypatch):
    from meridian.lib.ops import session_transcript

    project, root, key, _ = _capture_fixture(tmp_path, monkeypatch)
    read_source = session_transcript.iter_source_events

    def source_with_new_owner(source):
        yield from read_source(source)
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

    monkeypatch.setattr(session_transcript, "iter_source_events", source_with_new_owner)
    with pytest.raises(ValueError, match="active native owner"):
        materialize_native_history(project, root, key)
    assert not (root / "spawns" / key / "history.jsonl").exists()


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
        assert not (root / "spawns" / key / "history.jsonl").exists()
    finally:
        session_store.stop_session(root, "c2")
    # A new generation using the same c2 alias must not lend its lease to the
    # stopped generation whose state/sidecar supplied the matching native ID.
    session_store.start_session(root, "pi", "different-native", "test", chat_id="c2")
    try:
        materialize_native_history(project, root, key)
        assert (root / "spawns" / key / "history.jsonl").exists()
    finally:
        session_store.stop_session(root, "c2")


def test_archive_prepares_existing_legacy_child_stream_without_native_capture(
    tmp_path: Path, monkeypatch
):
    from meridian.lib.ops.session_archive import archive_history
    from meridian.lib.state.retention_archive import iter_archived_events

    project, root, _, _ = _capture_fixture(tmp_path, monkeypatch)
    key = spawn_store.start_spawn(
        root,
        chat_id="c2",
        harness="pi",
        kind="child",
        prompt="child",
        model="test",
        agent="coder",
    )
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    state = spawn_store.get_spawn(root, key)
    assert state is not None and state.history_id is not None
    legacy = root / "artifacts" / key / "history.jsonl"
    legacy.parent.mkdir(parents=True)
    events = [
        {"type": "message", "message": {"role": "assistant", "content": "first attempt"}},
        {"event_type": "meridian.attempt.completed", "attempt": 1},
        {"type": "message", "message": {"role": "assistant", "content": "retry answer"}},
    ]
    legacy.write_text("".join(json.dumps(event) + "\n" for event in events))
    before = legacy.read_bytes()
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
    assert [row["payload"] for row in retained] == events
    assert legacy.read_bytes() == before


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
    assert not (root / "spawns" / key / "history.jsonl").exists()


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
            assert not (root / "spawns" / key / "history.jsonl").exists()
        else:
            # Equal opaque session IDs in distinct harness namespaces do not match.
            materialize_native_history(project, root, key)
            assert (root / "spawns" / key / "history.jsonl").exists()
    finally:
        session_store.stop_session(root, "c2")
