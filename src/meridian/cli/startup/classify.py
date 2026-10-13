"""Import-cheap argv classification for Meridian startup."""

from __future__ import annotations

from collections.abc import Sequence

from meridian.cli.bootstrap import HARNESS_SHORTCUT_NAMES, iter_positional_tokens
from meridian.cli.startup.catalog import CommandCatalog, CommandDescriptor

_ROOT_HELP_FLAGS = frozenset({"--help", "-h", "--version", "-v"})


def _positional_tokens(argv: Sequence[str]) -> list[str]:
    """Extract command-position tokens using bootstrap's startup flag tables."""

    return [token for _, token in iter_positional_tokens(argv)]


def classify_invocation(
    argv: Sequence[str],
    catalog: CommandCatalog,
) -> CommandDescriptor | None:
    """Classify argv into a startup command descriptor without importing handlers."""

    tokens = _positional_tokens(argv)
    if not tokens and any(token in _ROOT_HELP_FLAGS for token in argv):
        return None

    if tokens and tokens[0] in HARNESS_SHORTCUT_NAMES:
        tokens = tokens[1:]
        if not tokens:
            return None

    return catalog.classify(tokens)


__all__ = ["classify_invocation"]
