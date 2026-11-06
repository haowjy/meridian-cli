"""Read the installed Claude CLI's version."""

import re
import subprocess
from collections.abc import Mapping

_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.+-]+)?")
_TIMEOUT_SECONDS = 10


def claude_version(env: Mapping[str, str]) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)`` from ``claude --version`` run in ``env``, or None.

    ``claude`` resolves through ``env``'s PATH, the binary a launch uses. The pattern is
    matched over stdout and stderr and tolerates a pre-release suffix (``2.1.320-beta``).
    None means the binary is missing, hung, or printed no version.
    """
    try:
        proc = subprocess.run(
            ("claude", "--version"),
            env=dict(env),
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION_PATTERN.search(f"{proc.stdout}\n{proc.stderr}")
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)
