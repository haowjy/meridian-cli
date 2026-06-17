"""Unified render-mode resolution for Meridian CLI."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from typing import Literal

from meridian.lib.core.depth import is_managed_meridian_session

RenderMode = Literal["agent", "human"]


def parse_render_mode(value: str | None) -> RenderMode:
    """Parse a ``--mode`` value or exit with a clear error."""

    if value is None or value == "":
        raise SystemExit("--mode requires a value")
    normalized = value.strip().lower()
    if normalized == "agent":
        return "agent"
    if normalized == "human":
        return "human"
    raise SystemExit(f"--mode must be one of: agent, human (got {value!r})")


def resolve_render_mode(
    *,
    forced: RenderMode | None,
    env: Mapping[str, str] | None = None,
    stdin_isatty: bool | None = None,
    stdout_isatty: bool | None = None,
) -> RenderMode:
    """Resolve agent vs human rendering for one CLI invocation.

    Precedence: explicit ``--mode`` > managed session env > human default.
    Managed sessions downshift to human when both stdin and stdout are TTYs.
    """

    if forced is not None:
        return forced
    source = os.environ if env is None else env
    if is_managed_meridian_session(source):
        stdin_tty = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty
        stdout_tty = sys.stdout.isatty() if stdout_isatty is None else stdout_isatty
        if stdin_tty and stdout_tty:
            return "human"
        return "agent"
    return "human"


def is_agent_render_mode(mode: RenderMode) -> bool:
    return mode == "agent"


def extract_forced_render_mode(argv: Sequence[str]) -> RenderMode | None:
    """Return the last forced mode from ``--mode`` tokens, validating conflicts."""

    forced: RenderMode | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--mode":
            if index + 1 >= len(argv):
                raise SystemExit("--mode requires a value")
            parsed = parse_render_mode(argv[index + 1])
            if forced is not None and parsed != forced:
                raise SystemExit("Cannot combine conflicting --mode values.")
            forced = parsed
            index += 2
            continue
        if token.startswith("--mode="):
            parsed = parse_render_mode(token.partition("=")[2])
            if forced is not None and parsed != forced:
                raise SystemExit("Cannot combine conflicting --mode values.")
            forced = parsed
            index += 1
            continue
        index += 1
    return forced


def strip_mode_flags(argv: Sequence[str]) -> list[str]:
    """Remove ``--mode`` global flags and their values from argv."""

    cleaned: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--mode":
            index += 2
            continue
        if token.startswith("--mode="):
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return cleaned


__all__ = [
    "RenderMode",
    "extract_forced_render_mode",
    "is_agent_render_mode",
    "parse_render_mode",
    "resolve_render_mode",
    "strip_mode_flags",
]
