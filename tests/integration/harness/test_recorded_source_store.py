"""Recorded source namespaces survive a changed launch environment."""

import json
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import LaunchIntent
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.request import SessionRequest
from tests.support.opencode_db import write_opencode_db_session

SID = "12345678-1234-4234-8234-123456789abc"


@pytest.mark.parametrize("harness", [HarnessId.CODEX, HarnessId.OPENCODE])
def test_recorded_resume_store(tmp_path: Path, harness: HarnessId) -> None:
    adapter = HarnessRegistry.with_defaults().get(harness)
    store = tmp_path / "source" / ("sessions" if harness == HarnessId.CODEX else "selected.sqlite")
    native = (
        store / f"rollout-2026-01-01T00-00-00-{SID}.jsonl" if harness == HarnessId.CODEX else store
    )
    native.parent.mkdir(parents=True)
    if harness == HarnessId.CODEX:
        native.write_text(json.dumps({"type": "session_meta", "payload": {"id": SID}}) + "\n")
    else:
        write_opencode_db_session(db_path=store, session_id=SID, messages=[])
    env = {"CODEX_HOME": str(tmp_path / "decoy"), "OPENCODE_HOME": str(tmp_path / "decoy")}
    session = SessionRequest(
        requested_harness_session_id=SID,
        source_native_store=str(store),
        continue_source_tracked=True,
    )
    plan = LaunchIntent("resume", SID)
    finalized = adapter.finalize_native_identity(
        plan,
        child_env=env,
        child_cwd=tmp_path,
        session=session,
        spawn_id=SpawnId("p1"),
        interactive=False,
    )
    assert finalized.native_store == str(store)
    assert finalized.source == native
    assert env["CODEX_HOME" if harness == HarnessId.CODEX else "OPENCODE_DB"] == str(
        store.parent if harness == HarnessId.CODEX else store
    )
    native.unlink()
    with pytest.raises(ValueError, match="native_transcript_missing"):
        adapter.finalize_native_identity(
            plan,
            child_env=env,
            child_cwd=tmp_path,
            session=session,
            spawn_id=SpawnId("p1"),
            interactive=False,
        )


def test_relative_codex_home_uses_child_cwd(tmp_path: Path) -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId.CODEX)
    env = {"CODEX_HOME": "relative-home"}
    assert adapter.native_store_for_launch(child_env=env, child_cwd=tmp_path,
        spawn_id=SpawnId("p1"), operation="resume", interactive=False) == str(
        tmp_path / "relative-home" / "sessions"
    )
    assert env["CODEX_HOME"] == "relative-home"


@pytest.mark.parametrize("filename", ["selected.sqlite", "storage"])
def test_opencode_database_override_ignores_default_decoy(tmp_path: Path, filename: str) -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId.OPENCODE)
    selected = tmp_path / "override" / filename
    default = tmp_path / "home" / "opencode.db"
    for path in (selected, default):
        write_opencode_db_session(db_path=path, session_id=SID, messages=[])
    env = {"OPENCODE_HOME": str(default.parent), "OPENCODE_DB": str(selected)}
    store = adapter.native_store_for_launch(child_env=env, child_cwd=tmp_path,
        spawn_id=SpawnId("p1"), operation="resume", interactive=False)
    assert store == str(selected)
    assert (
        adapter.resolve_native_session_file(
            session_id=SID,
            native_store=Path(store),
        )
        == selected
    )
    assert adapter.native_transcript_kind(selected) == "opencode_db"
    selected.unlink()
    assert (
        adapter.resolve_native_session_file(
            session_id=SID,
            native_store=Path(store),
        )
        is None
    )


def test_codex_symlink_store_remains_reopenable(tmp_path: Path) -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId.CODEX)
    home = tmp_path / "codex-home"
    home.mkdir()
    real_store = tmp_path / "shared-journals"
    real_store.mkdir()
    (home / "sessions").symlink_to(real_store, target_is_directory=True)
    (real_store / f"rollout-2026-01-01T00-00-00-{SID}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": SID}}) + "\n",
        )
    env = {"CODEX_HOME": str(home)}
    store = adapter.native_store_for_launch(child_env=env, child_cwd=tmp_path,
        spawn_id=SpawnId("p1"), operation="resume", interactive=False)
    session = SessionRequest(
        requested_harness_session_id=SID, source_native_store=store, continue_source_tracked=True
    )
    env["CODEX_HOME"] = str(tmp_path / "wrong")
    result = adapter.finalize_native_identity(
        LaunchIntent("resume", SID),
        child_env=env,
        child_cwd=tmp_path,
        session=session,
        spawn_id=SpawnId("p1"),
        interactive=False,
    )
    assert result.native_store == store
    assert env["CODEX_HOME"] == str(home)


def test_codex_unselectable_store_cannot_fall_through_to_sibling(tmp_path: Path) -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId.CODEX)
    sibling = tmp_path / "sessions"
    sibling.mkdir()
    (sibling / f"rollout-2026-01-01T00-00-00-{SID}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": SID}}) + "\n",
        )
    session = SessionRequest(
        requested_harness_session_id=SID,
        source_native_store=str(tmp_path / "different-store"),
        continue_source_tracked=True,
    )
    with pytest.raises(ValueError, match="native_transcript_missing"):
        adapter.finalize_native_identity(
            LaunchIntent("resume", SID),
            child_env={},
            child_cwd=tmp_path,
            session=session,
            spawn_id=SpawnId("p1"),
            interactive=False,
        )


@pytest.mark.parametrize("harness", [HarnessId.CLAUDE, HarnessId.CODEX])
@pytest.mark.parametrize("content", ["", "{", "{}\n", "wrong-id"])
def test_exact_sources_refuse_invalid_headers(
    tmp_path: Path, harness: HarnessId, content: str,
) -> None:
    import json

    from meridian.lib.core.native_identity import NativeEntryMismatch, NativeSessionUnavailable
    from meridian.lib.harness.claude_preflight import ensure_claude_session_accessible

    store = tmp_path / "sessions"
    store.mkdir()
    native = store / (
        f"{SID}.jsonl" if harness == HarnessId.CLAUDE
        else f"rollout-2026-01-01T00-00-00-{SID}.jsonl"
    )
    mismatch = content == "wrong-id"
    if mismatch:
        content = json.dumps(
            {"sessionId": "other"} if harness == HarnessId.CLAUDE
            else {"type": "session_meta", "payload": {"id": "other"}}
        ) + "\n"
    native.write_text(content)
    adapter = HarnessRegistry.with_defaults().get(harness)
    error = NativeEntryMismatch if mismatch else NativeSessionUnavailable
    with pytest.raises(error):
        adapter.resolve_native_session_file(
             session_id=SID, native_store=store,
        )
    with pytest.raises(error):
        if harness == HarnessId.CLAUDE:
            ensure_claude_session_accessible(
                SID, tmp_path / "child", source_native_store=store,
                target_config_root=tmp_path / "target",
            )
        else:
            adapter.finalize_native_identity(
                LaunchIntent("resume", SID),
                child_env={}, child_cwd=tmp_path,
                session=SessionRequest(
                    requested_harness_session_id=SID, source_native_store=str(store),
                    continue_source_tracked=True,
                ),
                spawn_id=SpawnId("p1"), interactive=False,
            )
