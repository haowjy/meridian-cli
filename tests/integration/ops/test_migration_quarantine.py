"""Migration succeeds even with active or quarantined spawns.

The active-spawn guard was removed because the new migration only writes
identity into meridian.toml — it no longer moves runtime directories, so
active spawns cannot cause data loss.
"""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.ops.migration import migrate_project_id
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.spawn_store import start_spawn


def test_migration_succeeds_with_active_and_quarantined_spawns(
    tmp_path: Path,
) -> None:
    project_id = "12345678-1234-1234-1234-123456789abc"
    project_root = tmp_path / "project"
    meridian_dir = project_root / ".meridian"
    meridian_dir.mkdir(parents=True)
    atomic_write_text(meridian_dir / "id", project_id)

    runtime_root = tmp_path / "user-home" / "projects" / project_id
    start_spawn(
        runtime_root,
        chat_id="chat-1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="valid active work",
    )
    quarantined_id = str(
        start_spawn(
            runtime_root,
            chat_id="chat-1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="invalid persisted work",
        )
    )
    state_path = runtime_root / "spawns" / quarantined_id / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["status"] = "zombie"
    atomic_write_text(state_path, json.dumps(payload))

    result = migrate_project_id(project_root)

    assert result.status == "migrated"
    assert result.old_id == project_id
    assert result.new_id == project_id
    assert runtime_root.is_dir()
