"""User-level state root resolution and project ID management."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import cast

from meridian.lib.config.preserving_edit import (
    mutate_project_config,
    project_config_transaction,
    set_project_id,
)
from meridian.lib.platform import IS_WINDOWS, get_home_path
from meridian.lib.state.wordgen import generate_project_id


def get_user_home() -> Path:
    """Return the user-level Meridian data directory.

    Resolution order:
    1. MERIDIAN_HOME env var if set
    2. Platform default:
       - Unix/macOS: ~/.meridian/
       - Windows: %LOCALAPPDATA%\\meridian\\
         (fallback: %USERPROFILE%\\AppData\\Local\\meridian\\)
    """

    override = os.getenv("MERIDIAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()

    if IS_WINDOWS:
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "meridian"

        user_profile = os.getenv("USERPROFILE", "").strip()
        if user_profile:
            return Path(user_profile) / "AppData" / "Local" / "meridian"

        return get_home_path() / "AppData" / "Local" / "meridian"

    return get_home_path() / ".meridian"


def get_project_id(project_root: Path) -> str | None:
    """Read the precedence-exempt project ID from ``meridian.toml``.

    Accepts any non-empty string — UUIDs, three-word IDs, or any future format.
    Returns None when the id file is missing, unreadable, or empty.
    """

    config_path = project_root / "meridian.toml"
    if not config_path.is_file():
        return None
    try:
        payload = cast("dict[str, object]", tomllib.loads(config_path.read_text(encoding="utf-8")))
    except OSError:
        return None
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in '{config_path.as_posix()}': {exc}") from exc
    project = payload.get("project")
    if project is None:
        return None
    if not isinstance(project, dict):
        raise ValueError(f"Invalid [project] table in '{config_path.as_posix()}'.")
    raw_id = cast("dict[str, object]", project).get("id")
    if raw_id is None:
        return None
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError(f"Invalid [project] id in '{config_path.as_posix()}'.")
    return raw_id.strip()


def get_project_id_with_legacy_fallback(project_root: Path) -> str | None:
    """Read project ID from ``meridian.toml``, falling back to ``.meridian/id``.

    Read-only callers use this so legacy projects resolve without migration.
    """

    return get_project_id(project_root) or _legacy_project_id(project_root)


def _legacy_project_id(project_root: Path) -> str | None:
    legacy_path = project_root / ".meridian" / "id"
    try:
        value = legacy_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def write_project_id(project_root: Path, project_id: str) -> None:
    """Append or create the machine-managed project identity atomically."""

    normalized = project_id.strip()
    mutate_project_config(
        project_root,
        get_user_home(),
        lambda text: (
            set_project_id(
                text,
                normalized,
                config_path=project_root / "meridian.toml",
            ),
            None,
        ),
    )


def get_or_create_project_id(project_root: Path) -> str:
    """Read or create the project ID in ``meridian.toml``.

    - If .meridian/id exists, migrate its value into meridian.toml
    - If not, generate a three-word ID (adjective-noun-noun), collision-check
      against existing context/ and projects/ directories, write atomically
    - Up to 10 retries on collision; raises RuntimeError if exhausted
    """

    project_id = get_project_id(project_root)
    if project_id is not None:
        return project_id

    with project_config_transaction(project_root, get_user_home()):
        project_id = get_project_id(project_root)
        if project_id is not None:
            return project_id

        legacy_id = _legacy_project_id(project_root)
        if legacy_id is not None:
            from meridian.lib.ops.migration import migrate_legacy_project_identity

            result = migrate_legacy_project_identity(project_root)
            if result.status == "blocked":
                raise RuntimeError(result.blocking_reason or "Project identity migration blocked")
            migrated_id = get_project_id(project_root)
            if migrated_id is None:
                raise RuntimeError("Legacy project identity migration did not write an identity.")
            return migrated_id

        user_home = get_user_home()
        for _ in range(10):
            candidate = generate_project_id()
            if (
                not (user_home / "context" / candidate).exists()
                and not (user_home / "projects" / candidate).exists()
            ):
                project_id = candidate
                break
        else:
            raise RuntimeError("Failed to generate a unique project ID after 10 attempts")

        write_project_id(project_root, project_id)
        return project_id


def get_project_home(project_id: str) -> Path:
    """Return the user-level project data directory.

    Returns: get_user_home() / "projects" / project_id
    """

    return get_user_home() / "projects" / project_id


def get_context_home(project_id: str) -> Path:
    """Return the user-level context directory.

    Returns: get_user_home() / "context" / project_id
    """

    return get_user_home() / "context" / project_id
