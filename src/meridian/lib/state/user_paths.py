"""User-level state root resolution and project ID management."""

from __future__ import annotations

import json
import os
import time
import tomllib
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import cast

from meridian.lib.platform import IS_WINDOWS, get_home_path
from meridian.lib.platform.atomic import atomic_write_text
from meridian.lib.state.wordgen import generate_project_id

_PROJECT_ID_COMMENT = "# managed by meridian — do not edit"
_IDENTITY_LOCK_NAME = ".meridian.toml.identity.lock"


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


def _legacy_project_id(project_root: Path) -> str | None:
    legacy_path = project_root / ".meridian" / "id"
    try:
        value = legacy_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


@contextmanager
def _identity_creation_lock(project_root: Path) -> Generator[None, None, None]:
    """Serialize identity creation with a transient file beside the config."""

    lock_path = project_root / _IDENTITY_LOCK_NAME
    project_root.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            break
        except FileExistsError:
            try:
                owner_pid = int(lock_path.read_text(encoding="ascii"))
                os.kill(owner_pid, 0)
            except (FileNotFoundError, ProcessLookupError, ValueError):
                with suppress(FileNotFoundError):
                    lock_path.unlink()
                continue
            except PermissionError:
                pass
            time.sleep(0.05)
    try:
        os.close(descriptor)
        yield
    finally:
        with suppress(FileNotFoundError):
            lock_path.unlink()


def _write_project_id_unlocked(project_root: Path, project_id: str) -> None:
    normalized = project_id.strip()
    if not normalized:
        raise ValueError("Project ID must not be empty.")
    existing_id = get_project_id(project_root)
    if existing_id is not None:
        if existing_id != normalized:
            raise ValueError("Project identity is immutable once assigned.")
        return

    config_path = project_root / "meridian.toml"
    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        payload = cast("dict[str, object]", tomllib.loads(content))
        if "project" in payload:
            raise ValueError("Existing [project] table has no valid id.")
        if not content or content.endswith("\n\n"):
            separator = ""
        else:
            separator = "\n" if content.endswith("\n") else "\n\n"
        encoded_id = json.dumps(normalized, ensure_ascii=False)
        updated = f"{content}{separator}{_PROJECT_ID_COMMENT}\n[project]\nid = {encoded_id}\n"
    else:
        encoded_id = json.dumps(normalized, ensure_ascii=False)
        updated = f"{_PROJECT_ID_COMMENT}\n[project]\nid = {encoded_id}\n"
    atomic_write_text(config_path, updated)


def write_project_id(project_root: Path, project_id: str) -> None:
    """Append or create the machine-managed project identity atomically."""

    with _identity_creation_lock(project_root):
        _write_project_id_unlocked(project_root, project_id)


def get_or_create_project_id(project_root: Path) -> str:
    """Read or create the project ID in ``meridian.toml``.

    - If .meridian/id exists, read and return it (any format accepted)
    - If not, generate a three-word ID (adjective-noun-noun), collision-check
      against existing context/ and projects/ directories, write atomically
    - Up to 10 retries on collision; raises RuntimeError if exhausted
    """

    project_id = get_project_id(project_root)
    if project_id is not None:
        return project_id

    with _identity_creation_lock(project_root):
        project_id = get_project_id(project_root)
        if project_id is not None:
            return project_id

        legacy_id = _legacy_project_id(project_root)
        if legacy_id is not None:
            from meridian.lib.ops.migration import migrate_legacy_project_identity

            result = migrate_legacy_project_identity(project_root, lock_held=True)
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

        _write_project_id_unlocked(project_root, project_id)
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
