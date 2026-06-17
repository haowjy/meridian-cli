"""Git subprocess helpers for hermetic temp-repo tests."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def is_git_env_key(key: str) -> bool:
    return key.upper().startswith("GIT_")


def isolated_git_env(
    *,
    base_env: Mapping[str, str] | None = None,
    global_config_path: Path | None = None,
) -> dict[str, str]:
    """Return a git env scrubbed of repo-targeting overrides.

    ``GIT_DIR``/``GIT_WORK_TREE`` and related variables can retarget commands
    into the real checkout. Strip inherited git env, then restore only the
    hermetic config overrides the suite wants.
    """

    env = dict(os.environ if base_env is None else base_env)
    for key in tuple(env):
        if is_git_env_key(key):
            env.pop(key, None)

    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if global_config_path is not None:
        global_config_path.parent.mkdir(parents=True, exist_ok=True)
        global_config_path.write_text("", encoding="utf-8")
        env["GIT_CONFIG_GLOBAL"] = str(global_config_path)
    return env
