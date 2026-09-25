"""Claude preparation reads the recorded file, never an ambient same-ID decoy."""
import json
import subprocess
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.claude_sessions import project_slug
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.request import SessionRequest


@pytest.mark.parametrize("missing", [False, True])
def test_recorded_claude_source_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    sid = "12345678-1234-4234-8234-123456789abc"
    cwd = tmp_path / "target"
    cwd.mkdir()
    store = tmp_path / "recorded" / "arbitrary-project-store"
    store.mkdir(parents=True)
    if not missing:
        (store / f"{sid}.jsonl").write_text(
            json.dumps({"sessionId": sid, "message": "RECORDED SOURCE"}) + "\n",
        )
    ambient = tmp_path / "ambient"
    decoy = ambient / "projects" / project_slug(cwd) / f"{sid}.jsonl"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("AMBIENT DECOY\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ambient))
    destination = tmp_path / "destination"
    adapter = HarnessRegistry.with_defaults().get(HarnessId.CLAUDE)
    session = SessionRequest(
        requested_harness_session_id=sid, source_native_store=str(store),
        continue_source_tracked=True, continue_source_ref="c1",
    )
    def prepare() -> None:
        adapter.prepare_prelaunch(
            runtime_root=tmp_path, spawn_id=SpawnId("p1"), session=session,
            child_cwd=cwd, child_env={"CLAUDE_CONFIG_DIR": str(destination)},
            resolved_harness_session_id=sid,
        )
    target = destination / "projects" / project_slug(cwd) / f"{sid}.jsonl"
    if missing:
        with pytest.raises(NativeSessionUnavailable) as caught:
            prepare()
        assert caught.value.reason == "missing"
        assert not target.exists()
    else:
        prepare()
        shim = tmp_path / "claude"
        shim.write_text('#!/bin/sh\ncat "$1"\n')
        result = subprocess.run(["sh", str(shim), str(target)], text=True, capture_output=True)
        assert result.returncode == 0
        assert result.stdout == json.dumps({"sessionId": sid, "message": "RECORDED SOURCE"}) + "\n"
