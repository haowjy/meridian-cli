"""Migration from legacy repo-local project identity to ``meridian.toml``."""

from __future__ import annotations

import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.state.user_paths import (
    _write_project_id_unlocked,
    get_project_home,
    get_project_id,
    write_project_id,
)


@dataclass(frozen=True)
class MigrationResult:
    """Result of a project identity migration attempt."""

    status: str
    old_id: str | None = None
    new_id: str | None = None
    moved_context: bool = False
    moved_runtime: bool = False
    blocking_reason: str | None = None
    removed_legacy_identity: bool = False
    removed_legacy_gitignore: bool = False


def _read_legacy_id(project_root: Path) -> str | None:
    try:
        value = (project_root / ".meridian" / "id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def migrate_legacy_project_identity(
    project_root: Path,
    *,
    lock_held: bool = False,
) -> MigrationResult:
    """Move repo-local identity into ``meridian.toml``, writing identity last."""

    existing_id = get_project_id(project_root)
    legacy_id = _read_legacy_id(project_root)
    if legacy_id is None:
        return MigrationResult(status="not-needed", old_id=existing_id, new_id=existing_id)
    if existing_id is not None and existing_id != legacy_id:
        return MigrationResult(
            status="blocked",
            old_id=legacy_id,
            new_id=existing_id,
            blocking_reason="meridian.toml and .meridian/id contain different project IDs",
        )

    blocking_spawns = _get_active_spawns(legacy_id)
    if blocking_spawns:
        return MigrationResult(
            status="blocked",
            old_id=legacy_id,
            blocking_reason=f"Active spawns: {', '.join(blocking_spawns)}",
        )

    # The user-home context/runtime roots already use this ID. Identity is the
    # only authoritative write and therefore remains last among durable writes.
    if existing_id is None:
        if lock_held:
            _write_project_id_unlocked(project_root, legacy_id)
        else:
            write_project_id(project_root, legacy_id)

    legacy_dir = project_root / ".meridian"
    legacy_identity = legacy_dir / "id"
    legacy_gitignore = legacy_dir / ".gitignore"
    removed_id = legacy_identity.exists()
    removed_gitignore = legacy_gitignore.exists()
    legacy_identity.unlink(missing_ok=True)
    legacy_gitignore.unlink(missing_ok=True)
    with suppress(OSError):
        legacy_dir.rmdir()

    return MigrationResult(
        status="migrated",
        old_id=legacy_id,
        new_id=legacy_id,
        removed_legacy_identity=removed_id,
        removed_legacy_gitignore=removed_gitignore,
    )


def migrate_project_id(project_root: Path) -> MigrationResult:
    """Public migration entry point."""

    return migrate_legacy_project_identity(project_root)


def _try_move_dir(src: Path, dst: Path) -> bool:
    """Retained for compatibility with migration tests and old state repair."""
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return True


def _get_active_spawns(project_id: str) -> list[str]:
    """Return active spawn IDs, failing open exactly as the legacy migration did."""
    try:
        from meridian.lib.state.spawn_store import ACTIVE_SPAWN_STATUSES, list_spawns

        runtime_root = get_project_home(project_id)
        if not runtime_root.exists():
            return []
        return [
            spawn.id for spawn in list_spawns(runtime_root) if spawn.status in ACTIVE_SPAWN_STATUSES
        ]
    except Exception:
        return []
