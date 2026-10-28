"""Fresh-process close workload shared by the import guard and RSS probe.

The caller supplies a private writable runtime; never point this at real state.
The native fixture is a stopped Pi transcript, so no harness process is launched.
"""

import json
import os
from pathlib import Path


def prepare_close(project: Path, root: Path) -> str:
    from meridian.lib.state import session_store, spawn_store

    os.environ["_MERIDIAN_RUNTIME_DIR"] = str(root)
    os.environ["MERIDIAN_HOME"] = str(root.parent.parent)
    project.mkdir(parents=True, exist_ok=True)
    (project / "meridian.toml").write_text(f'[project]\nid="{root.name}"\n')
    root.mkdir(parents=True, exist_ok=True)
    native = root / "probe-native"
    native.mkdir()
    key = spawn_store.start_spawn(
        root,
        spawn_id="p900000",
        chat_id="c900000",
        prompt="close probe",
        harness="pi",
        model="test",
        agent="",
        kind="primary",
        harness_session_id="probe-native",
    )
    session_store.start_session(
        root,
        "pi",
        "probe-native",
        "test",
        chat_id="c900000",
        kind="primary",
        spawn_id=key,
        native_store=str(native),
    )
    session_store.stop_session(root, "c900000")
    spawn_store.finalize_spawn(root, key, status="succeeded", exit_code=0, origin="runner")
    rows = [
        {"type": "session", "version": 3, "id": "probe-native", "cwd": str(project)},
        {
            "type": "message",
            "id": "a",
            "parentId": None,
            "message": {"role": "assistant", "content": "close probe complete"},
        },
    ]
    (native / "timestamp_probe-native.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    return key


def close(project: Path, root: Path, key: str) -> None:
    from meridian.lib.ops.session_archive import (
        materialize_native_history,
        session_stop_maintenance,
    )
    from meridian.lib.state.native_snapshot import complete_published_snapshot
    from meridian.lib.state.paths import resolve_project_runtime_root

    assert resolve_project_runtime_root(project) == root
    assert materialize_native_history(project, root, key) == ("pi", "probe-native")
    assert session_stop_maintenance(project, key) is None
    from meridian.lib.state.paths import NATIVE_SNAPSHOT_FILENAME

    snapshot = root / "spawns" / key / NATIVE_SNAPSHOT_FILENAME
    assert complete_published_snapshot(snapshot).state == "complete"
