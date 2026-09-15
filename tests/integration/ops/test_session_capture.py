"""Post-stop native capture is bound to the completed aggregate, not latest chat."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.harness.pi_paths import resolve_pi_spawn_session_root
from meridian.lib.ops.session_archive import session_stop_maintenance
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


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
