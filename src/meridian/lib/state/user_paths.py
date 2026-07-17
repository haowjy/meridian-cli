"""User-level state root resolution and project ID management."""

from __future__ import annotations

import os
from pathlib import Path

from meridian.lib.platform import IS_WINDOWS, get_home_path
from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.platform.locking import lock_file
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


def get_project_id(meridian_dir: Path) -> str | None:
    """Read-only project ID lookup from `.meridian/id`.

    Accepts any non-empty string — UUIDs, three-word IDs, or any future format.
    Returns None when the id file is missing, unreadable, or empty.
    """

    id_file = meridian_dir / "id"
    if not id_file.is_file():
        return None
    try:
        project_id = id_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return project_id or None


def get_or_create_project_id(meridian_dir: Path) -> str:
    """Read or generate the project ID from .meridian/id.

    - If .meridian/id exists, read and return it (any format accepted)
    - If not, generate a three-word ID (adjective-noun-noun), collision-check
      against existing context/ and projects/ directories, write atomically
    - Up to 10 retries on collision; raises RuntimeError if exhausted
    """

    project_id = get_project_id(meridian_dir)
    if project_id is not None:
        return project_id

    lock_path = meridian_dir / "id.lock"
    with lock_file(lock_path):
        project_id = get_project_id(meridian_dir)
        if project_id is not None:
            return project_id

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

        id_file = meridian_dir / "id"
        meridian_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(id_file, project_id)
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
