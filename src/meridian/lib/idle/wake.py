"""Pure reading of the work a turn left running: will it wake the session, and by when?"""

from __future__ import annotations

import math
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from meridian.lib.harness.idle_types import BackgroundTask

# Meridian options that may precede a subcommand and take a value.
_GLOBAL_VALUE_OPTIONS = frozenset({"-C", "--directory", "--config", "--format", "--mode"})
_YIELD_OPTION = "--yield-after-secs"


@dataclass(frozen=True)
class SpawnCommand:
    """A `meridian spawn ...` invocation found in a shell command line."""

    waits: bool
    # `spawn wait --yield-after-secs`; None means the configured default.
    yield_seconds: float | None = None


@dataclass(frozen=True)
class ExpectedWake:
    """What running work says about the session's next turn."""

    # Why running work is certain to wake the session (it ends and the harness
    # resumes the turn); None when nothing is certain.
    certain: str | None = None
    # A wake no later than this epoch-ms time, when one is known.
    by_ms: int | None = None
    # Other work may wake the session, or run for good (a dev server).
    maybe: bool = False


NO_WAKE = ExpectedWake()


def _segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    try:
        for token in lexer:
            if token and all(char in "();<>|&" for char in token):
                segments.append([])
            else:
                segments[-1].append(token)
    except ValueError:  # unbalanced quotes: not a command we can read
        return []
    return [segment for segment in segments if segment]


def _spawn_in(tokens: list[str]) -> SpawnCommand | None:
    for start, token in enumerate(tokens):
        if PurePosixPath(token).name != "meridian":
            continue
        index = start + 1
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 2 if tokens[index] in _GLOBAL_VALUE_OPTIONS else 1
        if index >= len(tokens) or tokens[index] != "spawn":
            return None
        rest = tokens[index + 1 :]
        if not rest or rest[0] != "wait":
            return SpawnCommand(waits=False)
        return SpawnCommand(waits=True, yield_seconds=_yield_seconds(rest[1:]))
    return None


def _yield_seconds(args: list[str]) -> float | None:
    for index, arg in enumerate(args):
        raw: str | None = None
        if arg == _YIELD_OPTION and index + 1 < len(args):
            raw = args[index + 1]
        elif arg.startswith(f"{_YIELD_OPTION}="):
            raw = arg.partition("=")[2]
        if raw is not None:
            try:
                seconds = float(raw)
            except ValueError:
                return None
            return seconds if math.isfinite(seconds) and seconds > 0 else None
    return None


def spawn_command(command: str) -> SpawnCommand | None:
    """Find a `meridian spawn` invocation in a shell command line, through any wrapper."""

    for segment in _segments(command):
        found = _spawn_in(segment)
        if found is not None:
            return found
    return None


def expected_wake(
    tasks: Iterable[BackgroundTask],
    *,
    now_ms: int,
    default_yield_seconds: float,
) -> ExpectedWake:
    """Reduce the running work to what it says about the next wake.

    Agents and `meridian spawn` shells end and wake the session; a `spawn wait`
    also yields by a known time. Watchers never count: they may stand forever
    and wake only on events.
    """

    agents = 0
    spawns = 0
    by_ms: int | None = None
    maybe = False
    for task in tasks:
        if task.kind == "agent":
            agents += 1
        elif task.kind == "watcher":
            continue
        elif task.kind == "shell" and (found := spawn_command(task.command or "")) is not None:
            spawns += 1
            if found.waits:
                seconds = found.yield_seconds or default_yield_seconds
                wake_ms = now_ms + int(seconds * 1000)
                by_ms = wake_ms if by_ms is None else min(by_ms, wake_ms)
        else:
            maybe = True
    if agents:
        certain: str | None = f"{agents} agent(s) running"
    elif spawns:
        certain = "meridian spawns running"
    else:
        certain = None
    return ExpectedWake(certain=certain, by_ms=by_ms, maybe=maybe)


__all__ = ["NO_WAKE", "ExpectedWake", "SpawnCommand", "expected_wake", "spawn_command"]
