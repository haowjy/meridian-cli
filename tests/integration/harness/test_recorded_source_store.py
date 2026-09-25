"""Recorded source namespaces survive a changed launch environment."""

from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeIdentityPlan
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
        native.write_text("{}\n")
    else:
        write_opencode_db_session(db_path=store, session_id=SID, messages=[])
    env = {"CODEX_HOME": str(tmp_path / "decoy"), "OPENCODE_HOME": str(tmp_path / "decoy")}
    session = SessionRequest(
        requested_harness_session_id=SID,
        source_native_store=str(store),
        continue_source_tracked=True,
    )
    plan = NativeIdentityPlan(SID, None, None, "resume")
    finalized = adapter.finalize_native_identity(
        plan,
        child_env=env,
        child_cwd=tmp_path,
        session=session,
        spawn_id=SpawnId("p1"),
        interactive=False,
    )
    assert finalized.native_store == str(store)
    assert finalized.locator == str(native)
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
    assert adapter.native_store_for_launch(child_env=env, child_cwd=tmp_path) == str(
        tmp_path / "relative-home" / "sessions"
    )
    assert env["CODEX_HOME"] == str(tmp_path / "relative-home")


@pytest.mark.parametrize("filename", ["selected.sqlite", "storage"])
def test_opencode_database_override_ignores_default_decoy(tmp_path: Path, filename: str) -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId.OPENCODE)
    selected = tmp_path / "override" / filename
    default = tmp_path / "home" / "opencode.db"
    for path in (selected, default):
        write_opencode_db_session(db_path=path, session_id=SID, messages=[])
    env = {"OPENCODE_HOME": str(default.parent), "OPENCODE_DB": str(selected)}
    store = adapter.native_store_for_launch(child_env=env, child_cwd=tmp_path)
    assert store == str(selected)
    assert (
        adapter.resolve_native_session_file(
            project_root=tmp_path,
            session_id=SID,
            native_store=Path(store),
        )
        == selected
    )
    assert adapter.native_transcript_kind(selected) == "opencode_db"
    selected.unlink()
    assert (
        adapter.resolve_native_session_file(
            project_root=tmp_path,
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
    (real_store / f"rollout-2026-01-01T00-00-00-{SID}.jsonl").write_text("{}\n")
    env = {"CODEX_HOME": str(home)}
    store = adapter.native_store_for_launch(child_env=env, child_cwd=tmp_path)
    session = SessionRequest(
        requested_harness_session_id=SID, source_native_store=store, continue_source_tracked=True
    )
    env["CODEX_HOME"] = str(tmp_path / "wrong")
    result = adapter.finalize_native_identity(
        NativeIdentityPlan(SID, None, None, "resume"),
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
    (sibling / f"rollout-2026-01-01T00-00-00-{SID}.jsonl").write_text("{}\n")
    session = SessionRequest(
        requested_harness_session_id=SID,
        source_native_store=str(tmp_path / "different-store"),
        continue_source_tracked=True,
    )
    with pytest.raises(ValueError, match="native_transcript_missing"):
        adapter.finalize_native_identity(
            NativeIdentityPlan(SID, None, None, "resume"),
            child_env={},
            child_cwd=tmp_path,
            session=session,
            spawn_id=SpawnId("p1"),
            interactive=False,
        )
