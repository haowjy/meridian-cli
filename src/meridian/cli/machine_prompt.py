"""Questions ``machine add`` asks, in a terminal only."""

from __future__ import annotations

import sys

from meridian.cli.invocation import get_global_options


class AddError(Exception):
    """``machine add`` stops here; nothing is saved."""


def is_terminal() -> bool:
    """Stdin and stderr are terminals and ``--no-input`` was not given."""

    return sys.stdin.isatty() and sys.stderr.isatty() and not get_global_options().no_input


def ask(question: str, *, default: bool) -> bool:
    """Ask yes or no; ``--yes`` takes the default. Enter takes it too, and an answer that is
    neither yes nor no ends ``add``, as in herdr."""

    if get_global_options().yes:
        return default
    print(f"{question} [{'Y/n' if default else 'y/N'}] ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline().strip().lower()
    if not answer:
        return default
    if answer in ("y", "yes"):
        return True
    if answer in ("n", "no"):
        return False
    raise AddError("expected yes or no")


def ask_text(question: str, default: str | None) -> str:
    """Ask for a line; with no *default*, an empty answer is empty. ``--yes`` takes the default."""

    if get_global_options().yes:
        return default or ""
    shown = f" [{default}]" if default else ""
    print(f"{question}{shown}: ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip() or (default or "")


__all__ = ["AddError", "ask", "ask_text", "is_terminal"]
