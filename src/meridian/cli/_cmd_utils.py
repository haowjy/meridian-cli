"""Shared helpers for CLI command handlers."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Maps bare alias names to the env-var that holds their resolved directory.
_CONTEXT_ALIASES: dict[str, str] = {
    "kb": "MERIDIAN_CONTEXT_KB_DIR",
    "strategy": "MERIDIAN_CONTEXT_STRATEGY_DIR",
    "work": "MERIDIAN_ACTIVE_WORK_DIR",
}


def resolve_fmt(fmt: str) -> str:
    """Return 'json' if the global --json flag is active, else the per-command fmt."""
    from meridian.cli.main import get_global_options

    return "json" if get_global_options().output.format == "json" else fmt


def resolve_context_alias(path: Path) -> Path:
    """If *path* is a bare context alias (e.g. ``kb``), resolve it via env var.

    A path qualifies as a bare alias when it has no directory-separator
    components (i.e. ``path.parent == Path(".")``).  If the alias env var is
    unset the command exits with a clear error message.  Unrecognised names
    are returned unchanged so normal filesystem lookup takes over.
    """
    if path.parent != Path("."):
        return path
    alias_env = _CONTEXT_ALIASES.get(path.name)
    if alias_env is None:
        return path
    value = os.environ.get(alias_env)
    if not value:
        print(
            f"Error: context alias '{path.name}' requires ${alias_env} to be set"
            " (run 'meridian context' to check)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return Path(value)


__all__ = [
    "resolve_context_alias",
    "resolve_fmt",
]
