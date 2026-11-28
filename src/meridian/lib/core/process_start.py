"""The environment and directory the process started with.

Startup changes both before a command runs: it sets ``MERIDIAN_PROJECT_DIR`` for every
command, ``-C`` removes the caller's session variables and changes directory, and
``--config`` sets ``MERIDIAN_CONFIG``. A program Meridian runs on the user's behalf must
see what it would see run directly, so the entry point records both first.
"""

from __future__ import annotations

import os
from pathlib import Path

_start_env: dict[str, str] | None = None
_start_cwd: Path | None = None


def capture_process_start() -> None:
    """Record the environment and directory now; the entry point calls it once, first."""

    global _start_env, _start_cwd
    _start_env = dict(os.environ)
    try:
        _start_cwd = Path.cwd()
    except OSError:
        _start_cwd = None


def starting_env() -> dict[str, str]:
    """A copy of the environment the process started with (the live one if never captured)."""

    return dict(os.environ if _start_env is None else _start_env)


def starting_cwd() -> Path | None:
    """The directory the process started in, ``None`` when it couldn't be read."""

    return Path.cwd() if _start_env is None else _start_cwd


__all__ = ["capture_process_start", "starting_cwd", "starting_env"]
