"""Project ID migration from UUID to three-word format."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.state.user_paths import (
    get_project_home,
    get_project_id,
    get_user_home,
)
from meridian.lib.state.wordgen import generate_project_id

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MigrationResult:
    """Result of a project ID migration attempt."""

    status: str  # "migrated", "not-needed", "blocked"
    old_id: str | None = None
    new_id: str | None = None
    moved_context: bool = False
    moved_runtime: bool = False
    blocking_reason: str | None = None


def is_uuid_format(project_id: str) -> bool:
    """Return True if the string matches UUID format."""
    return bool(_UUID_PATTERN.match(project_id))


def migrate_project_id(project_root: Path) -> MigrationResult:
    """Migrate a project from UUID ID to three-word ID.

    Steps:
    1. Read .meridian/id
    2. Check if it's UUID format — skip if already a word ID
    3. Check for active spawns — refuse if any
    4. Generate new three-word ID (with collision check)
    5. Move context dir if it exists
    6. Move runtime dir if it exists
    7. Update .meridian/id atomically (LAST)
    """
    meridian_dir = project_root / ".meridian"
    current_id = get_project_id(meridian_dir)

    if current_id is None:
        return MigrationResult(
            status="not-needed",
            blocking_reason="No project ID found",
        )

    if not is_uuid_format(current_id):
        return MigrationResult(
            status="not-needed",
            old_id=current_id,
        )

    # Check for active spawns before touching anything
    blocking_spawns = _get_active_spawns(current_id)
    if blocking_spawns:
        return MigrationResult(
            status="blocked",
            old_id=current_id,
            blocking_reason=f"Active spawns: {', '.join(blocking_spawns)}",
        )

    # Generate new three-word ID with collision check
    user_home = get_user_home()
    new_id: str | None = None
    for _ in range(10):
        candidate = generate_project_id()
        if (
            not (user_home / "context" / candidate).exists()
            and not (user_home / "projects" / candidate).exists()
        ):
            new_id = candidate
            break

    if new_id is None:
        return MigrationResult(
            status="blocked",
            old_id=current_id,
            blocking_reason="Failed to generate unique ID after 10 attempts",
        )

    # Move directories before updating the ID file
    moved_context = _try_move_dir(
        user_home / "context" / current_id,
        user_home / "context" / new_id,
    )
    moved_runtime = _try_move_dir(
        user_home / "projects" / current_id,
        user_home / "projects" / new_id,
    )

    # Update .meridian/id atomically — LAST
    atomic_write_text(meridian_dir / "id", new_id)

    return MigrationResult(
        status="migrated",
        old_id=current_id,
        new_id=new_id,
        moved_context=moved_context,
        moved_runtime=moved_runtime,
    )


def _try_move_dir(src: Path, dst: Path) -> bool:
    """Move directory if source exists. Return True if moved."""
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return True


def _get_active_spawns(project_id: str) -> list[str]:
    """Return list of active spawn IDs for this project. Empty if none or unreadable."""
    try:
        from meridian.lib.state.spawn_store import (
            ACTIVE_SPAWN_STATUSES,
            list_spawns,
        )

        runtime_root = get_project_home(project_id)
        if not runtime_root.exists():
            return []
        return [
            spawn.id for spawn in list_spawns(runtime_root) if spawn.status in ACTIVE_SPAWN_STATUSES
        ]
    except Exception:
        # Spawn store read failed — assume no active spawns and allow migration.
        return []
