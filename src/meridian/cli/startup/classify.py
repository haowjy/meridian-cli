"""Import-cheap argv classification for Meridian startup."""

from __future__ import annotations

from collections.abc import Sequence

from meridian.cli.bootstrap import (
    _TOP_LEVEL_BOOL_FLAGS,  # pyright: ignore[reportPrivateUsage]
    _TOP_LEVEL_VALUE_FLAGS,  # pyright: ignore[reportPrivateUsage]
    HARNESS_SHORTCUT_NAMES,
)
from meridian.cli.startup.catalog import CommandCatalog, CommandDescriptor

_ROOT_HELP_FLAGS = frozenset({"--help", "-h", "--version", "-v"})


def _split_before_passthrough(argv: Sequence[str]) -> list[str]:
    if "--" not in argv:
        return list(argv)
    return list(argv[: list(argv).index("--")])


def _positional_tokens(argv: Sequence[str]) -> list[str]:
    """Extract command-position tokens using bootstrap's startup flag tables."""

    tokens: list[str] = []
    args = _split_before_passthrough(argv)
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            tokens.append(token)
            index += 1
            continue
        if "=" in token:
            index += 1
            continue
        if token in _TOP_LEVEL_BOOL_FLAGS:
            index += 1
            continue
        if token in _TOP_LEVEL_VALUE_FLAGS:
            index += 2
            continue
        index += 1
    return tokens


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
