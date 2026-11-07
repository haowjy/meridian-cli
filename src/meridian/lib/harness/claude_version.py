import re
import subprocess
from collections.abc import Mapping

_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.+-]+)?")
_TIMEOUT_SECONDS = 10


def claude_version(env: Mapping[str, str]) -> tuple[int, int, int] | None:
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
