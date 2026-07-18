"""Migration from legacy repo-local project identity to ``meridian.toml``."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from meridian.lib.config.preserving_edit import project_config_transaction
from meridian.lib.state.user_paths import (
    get_project_id,
    get_user_home,
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
) -> MigrationResult:
    """Move repo-local identity into ``meridian.toml`` resumably."""

    with project_config_transaction(project_root, get_user_home()):
        return _migrate_legacy_project_identity_locked(project_root)


def _migrate_legacy_project_identity_locked(project_root: Path) -> MigrationResult:
    """Run migration while the reentrant project-config transaction is held."""

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

    # The legacy ID is the transition's completion marker. Keep it until the
    # committed identity and all generated legacy stragglers are settled so an
    # interrupted migration remains discoverable and safe to retry.
    if existing_id is None:
        write_project_id(project_root, legacy_id)

    legacy_dir = project_root / ".meridian"
    legacy_identity = legacy_dir / "id"
    legacy_gitignore = legacy_dir / ".gitignore"
    removed_id = legacy_identity.exists()
    removed_gitignore = legacy_gitignore.exists()
    legacy_gitignore.unlink(missing_ok=True)
    legacy_identity.unlink(missing_ok=True)
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


