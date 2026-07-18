"""Migration must fail closed when spawn authority contains quarantined rows."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.ops.migration import migrate_project_id
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.spawn_store import start_spawn


def test_migration_does_not_move_runtime_with_active_and_quarantined_spawns(
    tmp_path: Path,
) -> None:
    project_id = "12345678-1234-1234-1234-123456789abc"
    project_root = tmp_path / "project"
    meridian_dir = project_root / ".meridian"
    meridian_dir.mkdir(parents=True)
    atomic_write_text(meridian_dir / "id", project_id)

    runtime_root = tmp_path / "user-home" / "projects" / project_id
    valid_id = str(
        start_spawn(
            runtime_root,
            chat_id="chat-1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="valid active work",
        )
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

    assert result.status == "blocked"
    assert result.blocking_reason is not None
    assert valid_id in result.blocking_reason
    assert f"quarantined:{quarantined_id}" in result.blocking_reason
    assert runtime_root.is_dir()
    assert (meridian_dir / "id").read_text(encoding="utf-8") == project_id
