from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

from meridian.lib.platform import IS_WINDOWS, expand_home_path, get_home_path


def get_user_home(*, env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    override = source.get("MERIDIAN_HOME", "").strip()
    if override:
        return expand_home_path(override, env=env)

    if IS_WINDOWS:
        local_app_data = source.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "meridian"

        user_profile = source.get("USERPROFILE", "").strip()
        if user_profile:
            return Path(user_profile) / "AppData" / "Local" / "meridian"

        return get_home_path(env=env) / "AppData" / "Local" / "meridian"

    return get_home_path(env=env) / ".meridian"


def get_project_home(project_id: str) -> Path:

    return get_user_home() / "projects" / project_id


def get_dry_run_preview_root(*, user_home: Path | None = None) -> Path:

    return (user_home or get_user_home()) / "cache" / "dry-run"


def project_lifetime_lock_path(runtime_root: Path) -> Path:

    return runtime_root.parent / ".locks" / f"{runtime_root.name}.lock"


def ensure_project_lifetime_lock(runtime_root: Path) -> Path:
    lock_path = project_lifetime_lock_path(runtime_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileExistsError):
        lock_path.touch(exist_ok=False)
    return lock_path


def get_context_home(project_id: str, *, user_home: Path | None = None) -> Path:

    return (user_home or get_user_home()) / "context" / project_id
