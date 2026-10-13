"""Platform detection constants for cross-platform code."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from .unix_modules import DeferredUnixModule, fcntl, pty, select, termios, tty

IS_WINDOWS = sys.platform == "win32"
IS_POSIX = not IS_WINDOWS


def get_home_path(*, env: Mapping[str, str] | None = None) -> Path:
    """Return home directory, respecting HOME env var for test isolation.

    On POSIX, Path.home() already respects HOME.
    On Windows, Path.home() ignores HOME and queries Windows APIs.
    This function provides consistent behavior across platforms.
    """

    home = (os.environ if env is None else env).get("HOME", "").strip()
    if home:
        return Path(home).expanduser()
    if env is not None:
        # Explicit child environments must not fall back to ambient HOME.
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    return Path.home()


def expand_home_path(value: str, *, env: Mapping[str, str] | None = None) -> Path:
    """Expand the current user's tilde against the supplied environment."""
    if value == "~" or value.startswith("~/"):
        return get_home_path(env=env) / value[2:]
    return Path(value).expanduser()


__all__ = [
    "IS_POSIX",
    "IS_WINDOWS",
    "DeferredUnixModule",
    "expand_home_path",
    "fcntl",
    "get_home_path",
    "pty",
    "select",
    "termios",
    "tty",
]
