"""Cross-platform command string rendering helpers."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence

from meridian.lib.platform import IS_WINDOWS


def format_command_for_display(argv: Sequence[str]) -> str:
    """Render argv as a deterministic shell command string for the active OS."""

    command = [str(part) for part in argv]
    if not command:
        return ""
    if IS_WINDOWS:
        return subprocess.list2cmdline(command)
    return shlex.join(command)


__all__ = ["format_command_for_display"]
