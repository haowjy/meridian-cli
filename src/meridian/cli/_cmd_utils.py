"""Shared helpers for CLI command handlers."""

from __future__ import annotations

import sys
from pathlib import Path

from meridian.cli.invocation import get_global_options


def resolve_fmt(fmt: str) -> str:
    """Return 'json' if the global --json flag is active, else the per-command fmt."""
    return "json" if get_global_options().output.format == "json" else fmt


def resolve_context_alias(path: Path) -> Path:
    """Return *path*, or the current context directory when it names a context.

    Anything that exists is always itself. Otherwise a bare name that matches a
    configured context (``kb``, ``work``, ``work.archive``, an extra context) resolves
    the way ``meridian context <name>`` does, never from the launch-time env var.
    Any other path is returned unchanged for normal filesystem lookup.
    """
    if path.exists() or path.parent != Path("."):
        return path

    from meridian.lib.ops.context import context_dir_sync

    name = path.name
    try:
        value = context_dir_sync(name)
    except KeyError:
        return path
    if not value:
        print(
            f"Error: '{name}' is not a directory here, and context '{name}' is unresolved"
            " (run 'meridian context' to check)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    resolved = Path(value)
    if not resolved.is_dir():
        print(
            f"Error: '{name}' is not a directory here, and context '{name}' resolves to"
            f" {value}, which does not exist",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return resolved


__all__ = [
    "resolve_context_alias",
    "resolve_fmt",
]
