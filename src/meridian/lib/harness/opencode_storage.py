"""OpenCode storage path helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path

from meridian.lib.platform import IS_WINDOWS, get_home_path


def _resolve_default_home(env: Mapping[str, str]) -> Path:
    """Resolve default home from launch env before parent-process fallback."""

    if IS_WINDOWS:
        user_profile = env.get("USERPROFILE", "").strip()
        if user_profile:
            return Path(user_profile).expanduser()

    home = env.get("HOME", "").strip()
    if home:
        return Path(home).expanduser()

    return get_home_path()


def resolve_opencode_data_root(launch_env: Mapping[str, str] | None = None) -> Path:
    """Resolve the parent directory of OpenCode's data folder.

    Precedence: ``XDG_DATA_HOME`` → ``%LOCALAPPDATA%`` on Windows → POSIX default.
    """

    env = launch_env if launch_env is not None else os.environ

    xdg_data_home = env.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home).expanduser()

    if IS_WINDOWS:
        local_app_data = env.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data)
        return _resolve_default_home(env) / "AppData" / "Local"

    return _resolve_default_home(env) / ".local" / "share"


def resolve_opencode_home_dir(launch_env: Mapping[str, str] | None = None) -> Path:
    """Resolve OpenCode's data directory (``opencode.db``, ``storage/``, ``log/``)."""

    env = launch_env if launch_env is not None else os.environ

    opencode_home = env.get("OPENCODE_HOME", "").strip()
    if opencode_home:
        return Path(opencode_home).expanduser()

    return resolve_opencode_data_root(launch_env) / "opencode"


def resolve_opencode_storage_root(launch_env: Mapping[str, str] | None = None) -> Path:
    """Resolve OpenCode storage root from launch env with platform fallback."""

    return resolve_opencode_home_dir(launch_env) / "storage"


def iter_opencode_session_files(storage_root: Path) -> Iterator[Path]:
    """Yield OpenCode session JSON files from known storage directories."""

    for dirname in ("session_diff", "session"):
        candidate_dir = storage_root / dirname
        if not candidate_dir.is_dir():
            continue
        yield from candidate_dir.glob("*.json")


def opencode_session_id_from_path(path: Path) -> str | None:
    """Return session id inferred from one OpenCode session filename."""

    if path.suffix != ".json":
        return None
    session_id = path.stem.strip()
    if not session_id:
        return None
    return session_id


def resolve_opencode_session_file(
    *,
    session_id: str,
    launch_env: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve one OpenCode session file by id from known storage directories."""

    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        return None

    storage_root = resolve_opencode_storage_root(launch_env)
    for dirname in ("session_diff", "session"):
        candidate = storage_root / dirname / f"{normalized_session_id}.json"
        if candidate.is_file():
            return candidate
    return None


__all__ = [
    "iter_opencode_session_files",
    "opencode_session_id_from_path",
    "resolve_opencode_data_root",
    "resolve_opencode_home_dir",
    "resolve_opencode_session_file",
    "resolve_opencode_storage_root",
]
