"""Recorded source namespaces survive a changed launch environment."""

from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeIdentityPlan
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.request import SessionRequest

SID = "12345678-1234-4234-8234-123456789abc"


@pytest.mark.parametrize("harness", [HarnessId.CODEX, HarnessId.OPENCODE])
def test_recorded_resume_store(tmp_path: Path, harness: HarnessId) -> None:
    adapter = HarnessRegistry.with_defaults().get(harness)
    store = tmp_path / "source" / ("sessions" if harness == HarnessId.CODEX else "storage")
    native = (
        store / f"rollout-2026-01-01T00-00-00-{SID}.jsonl"
        if harness == HarnessId.CODEX
        else store / "session" / f"{SID}.json"
    )
    native.parent.mkdir(parents=True)
    native.write_text("{}\n")
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
    assert env["CODEX_HOME" if harness == HarnessId.CODEX else "OPENCODE_HOME"] == str(store.parent)
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
