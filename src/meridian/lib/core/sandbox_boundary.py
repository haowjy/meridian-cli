"""Harness-sandbox boundary facts shared by CLI and spawn policy."""

from __future__ import annotations

import os

from meridian.lib.platform.pid_namespace import current_pid_namespace

LAUNCHER_PID_NAMESPACE_ENV = "_MERIDIAN_LAUNCHER_PID_NAMESPACE"
SANDBOXED_SPAWN_LAUNCH_ERROR = (
    "Cannot launch a child spawn from inside the parent shell sandbox. "
    "Run `meridian spawn` as a standalone shell command: no `cd ... &&`, pipes, `$()`, "
    "or `VAR=1` prefix."
)


def refuse_sandboxed_spawn_launch(*, dry_run: bool) -> None:
    """Refuse a launch that crossed from its harness launcher into a PID sandbox."""

    if dry_run:
        return
    launcher_pid_namespace = os.environ.get(LAUNCHER_PID_NAMESPACE_ENV, "").strip()
    if not launcher_pid_namespace:
        return
    current_namespace = current_pid_namespace()
    if current_namespace is None or launcher_pid_namespace == current_namespace:
        return
    raise ValueError(SANDBOXED_SPAWN_LAUNCH_ERROR)


__all__ = [
    "LAUNCHER_PID_NAMESPACE_ENV",
    "SANDBOXED_SPAWN_LAUNCH_ERROR",
    "refuse_sandboxed_spawn_launch",
]
