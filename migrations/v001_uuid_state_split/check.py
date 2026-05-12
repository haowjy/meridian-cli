#!/usr/bin/env python3
"""Check if v001 UUID state split migration is needed."""

import json
import sys
from pathlib import Path


def check(repo_root: Path) -> dict:
    """Return migration status for this repo."""

    meridian_dir = repo_root / ".meridian"

    # Check if UUID model is active
    uuid_file = meridian_dir / "id"
    if not uuid_file.is_file():
        return {"status": "not_applicable", "reason": "No .meridian/id — UUID model not active yet"}

    # Check if legacy state exists
    legacy_spawns = meridian_dir / "spawns.jsonl"
    legacy_sessions = meridian_dir / "sessions.jsonl"
    legacy_spawns_dir = meridian_dir / "spawns"

    has_legacy = legacy_spawns.is_file() or legacy_sessions.is_file() or legacy_spawns_dir.is_dir()

    if not has_legacy:
        return {
            "status": "not_applicable",
            "reason": "No legacy state in .meridian/ — fresh project or already migrated",
        }

    # Check if already migrated
    tracking_file = meridian_dir / ".migrations.json"
    if tracking_file.is_file():
        try:
            tracking = json.loads(tracking_file.read_text())
            if "v001" in tracking.get("applied", []):
                return {
                    "status": "done",
                    "reason": "v001 already applied according to .migrations.json",
                }
        except (json.JSONDecodeError, OSError):
            pass

    # Migration needed
    legacy_items = []
    if legacy_spawns.is_file():
        legacy_items.append("spawns.jsonl")
    if legacy_sessions.is_file():
        legacy_items.append("sessions.jsonl")
    if legacy_spawns_dir.is_dir():
        legacy_items.append("spawns/")

    return {"status": "needed", "reason": f"Legacy state found: {', '.join(legacy_items)}"}


if __name__ == "__main__":
    repo_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    result = check(repo_root)
    print(json.dumps(result, indent=2))
