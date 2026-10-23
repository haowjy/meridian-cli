"""Linux PID-namespace observation for sandbox-boundary checks."""

from __future__ import annotations

import os

_PID_NAMESPACE_PATH = "/proc/self/ns/pid"


def current_pid_namespace() -> str | None:
    """Return this process's PID-namespace identity when procfs exposes it."""

    try:
        return os.readlink(_PID_NAMESPACE_PATH)
    except OSError:
        return None


__all__ = ["current_pid_namespace"]
