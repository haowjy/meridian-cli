"""Thin CLI entrypoint for startup-cheap fast paths."""

from __future__ import annotations

from collections.abc import Sequence

from meridian.cli.bootstrap import first_positional_token, meridian_managed_env_default
from meridian.cli.mode import extract_forced_render_mode, is_agent_render_mode, resolve_render_mode
from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.classify import classify_invocation
from meridian.lib.platform import IS_WINDOWS


def _is_root_help_request(argv: Sequence[str]) -> bool:
    """Return true when argv requests root help before a command path."""

    if not any(token in {"--help", "-h"} for token in argv):
        return False
    return first_positional_token(argv) is None


def _is_version_request(argv: Sequence[str]) -> bool:
    """Return true when argv requests the root version before a command path."""

    if not any(token in {"--version", "-v"} for token in argv):
        return False
    return first_positional_token(argv) is None


def main() -> None:
    """Thin CLI entrypoint: handles trivial fast paths, delegates the rest."""

    with meridian_managed_env_default():
        _main_impl()


def _main_impl() -> None:
    import sys

    if IS_WINDOWS:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]

    # Keep classifier import/use on this startup-cheap path so command catalog
    # regressions surface before the full CLI tree is loaded.
    _ = classify_invocation(args, COMMAND_CATALOG)

    if _is_root_help_request(args):
        from meridian.cli.startup.help import render_root_help

        forced_mode = extract_forced_render_mode(args)
        render_mode = resolve_render_mode(
            forced=forced_mode,
            stdin_isatty=sys.stdin.isatty(),
            stdout_isatty=sys.stdout.isatty(),
        )
        print(
            render_root_help(agent_mode=is_agent_render_mode(render_mode)),
            end="",
        )
        return

    if _is_version_request(args):
        # Validate --mode on this fast path too, so an invalid/conflicting mode
        # errors consistently instead of being silently ignored before --version.
        extract_forced_render_mode(args)
        from meridian import __version__

        print(f"meridian {__version__}")
        return

    from meridian.cli.main import main as full_main

    full_main(argv=args)


__all__ = [
    "_is_root_help_request",
    "_is_version_request",
    "main",
]
