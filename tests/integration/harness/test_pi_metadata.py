"""Pi runtime metadata respects the published spawn's lifetime."""

import json
import shutil
from pathlib import Path

from meridian.lib.harness.pi import _write_pi_runtime_metadata_sidecar
from meridian.lib.state.paths import resolve_spawn_dir
from meridian.lib.state.spawn_store import start_spawn


def test_pi_metadata_never_recreates_missing_spawn(tmp_path: Path) -> None:
    payload = {"runtime_path": "/runtime/pi"}
    launch = start_spawn(
        tmp_path,
        spawn_id="metadata-owner",
        chat_id="chat-1",
        model="test",
        agent="tester",
        harness="pi",
        prompt="test",
        status="running",
    )
    _write_pi_runtime_metadata_sidecar(launch=launch, payload=payload)
    assert json.loads(launch.pi_runtime_meta.read_text(encoding="utf-8")) == payload

    shutil.rmtree(resolve_spawn_dir(tmp_path, launch.spawn_id))
    _write_pi_runtime_metadata_sidecar(launch=launch, payload=payload)
    assert not resolve_spawn_dir(tmp_path, launch.spawn_id).exists()
