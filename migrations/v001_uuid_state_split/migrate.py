#!/usr/bin/env python3
"""v001: Migrate runtime state from repo to user-level directory."""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Import from meridian for user paths resolution
# NOTE: This script must be run with meridian installed/available
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from migrations.v001_uuid_state_split.check import check


def migrate(repo_root: Path) -> dict:
    """Run the v001 migration."""

    # Check if needed
    status = check(repo_root)
    if status["status"] != "needed":
        return status

    meridian_dir = repo_root / ".meridian"

    # Get UUID and user state root
    uuid_file = meridian_dir / "id"
    project_uuid = uuid_file.read_text().strip()

    # Import here to avoid import errors if meridian not installed
    from meridian.lib.state.user_paths import get_project_home

    user_root = get_project_home(project_uuid)
    user_root.mkdir(parents=True, exist_ok=True)

    migrated = []

    # Migrate spawns.jsonl
    legacy_spawns = meridian_dir / "spawns.jsonl"
    if legacy_spawns.is_file():
        target = user_root / "spawns.jsonl"
        if not target.exists():
            shutil.copy2(legacy_spawns, target)
            migrated.append("spawns.jsonl")

    # Migrate sessions.jsonl
    legacy_sessions = meridian_dir / "sessions.jsonl"
    if legacy_sessions.is_file():
        target = user_root / "sessions.jsonl"
        if not target.exists():
            shutil.copy2(legacy_sessions, target)
            migrated.append("sessions.jsonl")

    # Migrate spawns/ directory
    legacy_spawns_dir = meridian_dir / "spawns"
    if legacy_spawns_dir.is_dir():
        target_dir = user_root / "spawns"
        if not target_dir.exists():
            shutil.copytree(legacy_spawns_dir, target_dir)
            migrated.append("spawns/")

    # Update tracking in repo
    _update_tracking(meridian_dir / ".migrations.json", "v001")

    # Update tracking in user root
    _update_tracking(user_root / ".migrations.json", "v001")

    return {"status": "ok", "migrated": migrated, "destination": str(user_root)}


def _update_tracking(tracking_file: Path, migration_id: str) -> None:
    """Update .migrations.json with applied migration."""

    if tracking_file.is_file():
        try:
            tracking = json.loads(tracking_file.read_text())
        except (json.JSONDecodeError, OSError):
            tracking = {"applied": [], "history": []}
    else:
        tracking = {"applied": [], "history": []}

    if migration_id not in tracking["applied"]:
        tracking["applied"].append(migration_id)
        tracking["history"].append(
            {
                "id": migration_id,
                "applied_at": datetime.now(datetime.UTC).isoformat(),
                "result": "ok",
            }
        )
        tracking_file.parent.mkdir(parents=True, exist_ok=True)
        tracking_file.write_text(json.dumps(tracking, indent=2))


if __name__ == "__main__":
    repo_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    result = migrate(repo_root)
    print(json.dumps(result, indent=2))
