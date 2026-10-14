"""Capability probes for optional Mars CLI features."""

from __future__ import annotations

import subprocess
from functools import cache


@cache
def mars_supports_fallback_root(executable: str | None) -> bool:
    """Return whether *executable* supports layered content lookup.

    The help surface is the compatibility contract.  Probe failures degrade to
    the pre-layering behavior, and the result is cached once per executable for
    the lifetime of this process.
    """

    if executable is None:
        return False
    try:
        result = subprocess.run(
            [executable, "build", "launch-bundle", "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return "--fallback-root" in f"{result.stdout}\n{result.stderr}"


__all__ = ["mars_supports_fallback_root"]
