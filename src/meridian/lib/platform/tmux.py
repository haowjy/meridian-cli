"""Best-effort terminal location lookup shared by launch and notifications."""

import subprocess


def resolve_tmux_target(pane: str) -> str | None:
    if not pane:
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#S:#W"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def resolve_tmux_session(pane: str) -> str | None:
    target = resolve_tmux_target(pane)
    return target.split(":", 1)[0] if target else None
