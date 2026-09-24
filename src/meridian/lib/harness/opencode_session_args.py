"""Bounded OpenCode grammar for primary native-session passthrough arguments."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NoReturn

from meridian.lib.harness.native_session_args import (
    NativeSessionSelector,
    NativeSessionSurface,
    NormalizedNativeSessionArgs,
    PrimaryArgControls,
)
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch

_SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]+\Z")
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARN", "ERROR"})
_SUBPROCESS_VALUE_OPTIONS = frozenset(
    {"--model", "-m", "--agent", "--variant", "--log-level"}
)
_MANAGED_VALUE_OPTIONS = frozenset({"--log-level"})
_ENDPOINT_OPTIONS = frozenset(
    {
        "--server",
        "--hostname",
        "--port",
        "--cwd",
        "--directory",
        "--session-dir",
        "--config",
        "--config-dir",
        "--url",
        "--endpoint",
    }
)


def _refuse(spelling: str, reason: str, alternative: str | None = None) -> NoReturn:
    suffix = f"; use {alternative}" if alternative else ""
    raise HarnessCapabilityMismatch(
        f"Unsupported OpenCode raw argument {spelling!r}: {reason}{suffix}."
    )


def _value(
    args: Sequence[str], index: int, spelling: str, inline_value: str | None
) -> tuple[str, int]:
    if inline_value is not None:
        value = inline_value
        next_index = index + 1
    elif index + 1 < len(args):
        value = args[index + 1]
        next_index = index + 2
    else:
        _refuse(spelling, "missing value")
    if not value.strip() or (inline_value is None and value.startswith("-")):
        _refuse(spelling, "missing or flag-like value")
    return value, next_index


def normalize_primary_session_args(
    args: Sequence[str],
    surface: NativeSessionSurface = "subprocess",
    *,
    controls: PrimaryArgControls | None = None,
) -> NormalizedNativeSessionArgs:
    """Normalize explicit OpenCode session selectors and retain a safe raw tail.

    The managed surface starts ``opencode serve`` rather than ``opencode run``;
    it therefore admits only the global logging flags in its raw remainder.
    """
    _ = controls
    if surface not in ("subprocess", "managed"):
        raise ValueError(f"Unknown OpenCode primary surface: {surface!r}")

    remaining: list[str] = []
    selector: NativeSessionSelector | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            _refuse("--", "positional and option-terminator forms are unsupported")
        if token.startswith("@"):
            _refuse("@file", "response-file indirection is unsupported")

        name, equals, inline = token.partition("=")
        inline_value = inline if equals else None
        if name in ("--session", "-s"):
            if name == "-s" and equals:
                _refuse("-s", "short option values must be separated")
            value, index = _value(args, index, name, inline_value)
            if not _SESSION_ID.fullmatch(value):
                _refuse(name, "expected an exact native session ID")
            if selector is not None:
                _refuse(name, "duplicate native session selector")
            selector = NativeSessionSelector("resume", value)
            continue
        if name in ("--continue", "-c"):
            _refuse(name, "implicit newest-session selection is unsupported")
        if name == "--fork":
            _refuse(name, "raw fork is unsupported; use Meridian's typed --fork option")
        if name in _ENDPOINT_OPTIONS:
            _refuse(name, "runtime, endpoint, cwd, or store overrides are unsupported")
        if name in ("--profile", "-p"):
            _refuse(
                name,
                "raw profiles are unresolved configuration indirection; "
                "use typed resolved settings",
            )
        if name in ("run", "attach") or not token.startswith("-"):
            spelling = "subcommand" if name in ("run", "attach") else "positional argument"
            _refuse(spelling, "positional arguments and subcommand injection are unsupported")

        if name == "--print-logs":
            if equals:
                _refuse(name, "this is a flag and does not take a value")
            remaining.append(token)
            index += 1
            continue
        allowed_value_options = (
            _SUBPROCESS_VALUE_OPTIONS
            if surface == "subprocess"
            else _MANAGED_VALUE_OPTIONS
        )
        if name in allowed_value_options:
            if name in ("-m",) and equals:
                _refuse(name, "short option values must be separated")
            value, next_index = _value(args, index, name, inline_value)
            if name == "--log-level" and value not in _LOG_LEVELS:
                _refuse(name, "expected DEBUG, INFO, WARN, or ERROR")
            remaining.extend(args[index:next_index])
            index = next_index
            continue

        if surface == "managed" and name in _SUBPROCESS_VALUE_OPTIONS:
            alternative = {
                "--model": "Meridian's typed model setting",
                "-m": "Meridian's typed model setting",
                "--agent": "Meridian's typed agent setting",
                "--variant": "Meridian's typed variant setting",
            }[name]
            _refuse(name, "run-only options cannot be forwarded to `opencode serve`", alternative)
        _refuse("unknown option", "unsupported option or option arity")

    return NormalizedNativeSessionArgs(selector, tuple(remaining))


__all__ = ["normalize_primary_session_args"]
