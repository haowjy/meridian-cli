"""Platform detection constants for cross-platform code."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .unix_modules import DeferredUnixModule, fcntl, pty, select, termios, tty

IS_WINDOWS = sys.platform == "win32"
IS_POSIX = not IS_WINDOWS


def get_home_path() -> Path:
    """Return home directory, respecting HOME env var for test isolation.

    On POSIX, Path.home() already respects HOME.
    On Windows, Path.home() ignores HOME and queries Windows APIs.
    This function provides consistent behavior across platforms.
    """

    home = os.environ.get("HOME", "").strip()
    if home:
        return Path(home).expanduser()
    return Path.home()


__all__ = [
    "IS_POSIX",
    "IS_WINDOWS",
    "DeferredUnixModule",
    "fcntl",
    "get_home_path",
    "pty",
    "select",
    "termios",
    "tty",
]
