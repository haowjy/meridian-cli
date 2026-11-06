"""Orphan-run repair captures the native transcript of a reconciled primary."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING


from meridian.lib.harness.pi_paths import resolve_pi_spawn_session_root
from meridian.lib.ops import diag
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.support.launch_paths import launch_paths

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_repair_captures_every_orphan_primary_of_one_native_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crashed ``--continue`` generations share a native id; one pass captures all.

    The real capture guard defers while any row bound to the native session is
    still active, so every orphan must be reconciled before any capture runs.
    """
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    project = tmp_path / "repo"
    project.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project)
    native_root = resolve_pi_spawn_session_root()
    native_root.mkdir(parents=True)
    (native_root / "timestamp_resumed-native.jsonl").write_text(
        json.dumps({"type": "session", "version": 3, "id": "resumed-native"}) + "\n"
    )
    spawn_ids = ("p1", "p2", "p3")
    for spawn_id in spawn_ids:
        spawn_store.start_spawn(
            runtime_root,
            spawn_id=spawn_id,
            chat_id="c1",
            harness="pi",
            harness_session_id="resumed-native",
            kind="primary",
            prompt="continued",
            model="test",
            agent="coder",
            worker_pid=222,
            runner_pid=111,
            started_at="2000-01-01T00:00:00Z",
        )
        (runtime_root / "spawns" / spawn_id / "launch.lock").touch()
        session_store.start_session(
            runtime_root,
            "pi",
            "resumed-native",
            "test",
            chat_id="c1",
            kind="primary",
            spawn_id=spawn_id,
            native_store=str(native_root),
        )
        session_store.stop_session(runtime_root, "c1")
    count, ids = diag._repair_orphan_runs(project, runtime_root=runtime_root)

    assert (count, ids) == (3, spawn_ids)
    for spawn_id in spawn_ids:
        record = spawn_store.get_spawn(runtime_root, spawn_id)
        assert record is not None and record.status == "failed"
        assert (launch_paths(runtime_root, spawn_id).native_transcript).is_file()
