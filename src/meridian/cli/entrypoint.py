"""Thin CLI entrypoint for startup-cheap fast paths."""

from __future__ import annotations

from collections.abc import Sequence

from meridian.cli.bootstrap import first_positional_token, meridian_managed_env_default
from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.classify import classify_invocation


def _validate_root_mode_flags(argv: Sequence[str]) -> None:
    """Reject mutually exclusive root help mode flags on startup-fast paths."""

    if "--agent" in argv and "--human" in argv and first_positional_token(argv) is None:
        import sys

        print("Cannot combine --agent with --human.", file=sys.stderr)
        raise SystemExit(1)


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

    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]

    # Keep classifier import/use on this startup-cheap path so command catalog
    # regressions surface before the full CLI tree is loaded.
    _ = classify_invocation(args, COMMAND_CATALOG)
    _validate_root_mode_flags(args)

    if _is_root_help_request(args):
        from meridian.cli.startup.help import detect_agent_mode, render_root_help

        force_agent = "--agent" in args
        force_human = "--human" in args
        print(
            render_root_help(
                agent_mode=detect_agent_mode(
                    force_agent=force_agent,
                    force_human=force_human,
                )
            ),
            end="",
        )
        return

    if _is_version_request(args):
        from meridian import __version__

        print(f"meridian {__version__}")
        return

    from meridian.cli.main import main as full_main

    full_main(argv=args)


__all__ = [
    "_is_root_help_request",
    "_is_version_request",
    "_validate_root_mode_flags",
    "main",
]
