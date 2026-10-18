"""Pure reading of the work a turn left running: will it wake the session, and by when?"""

from __future__ import annotations

import math
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from meridian.lib.harness.idle_types import BackgroundTask

# Meridian options that may precede a subcommand and take a value.
_GLOBAL_VALUE_OPTIONS = frozenset({"-C", "--directory", "--config", "--format", "--mode"})
_LOOP_COMMANDS = frozenset({"while", "until", "for", "select"})
_SPAWN_SUBCOMMANDS = frozenset(
    {
        "cancel",
        "cancel-all",
        "children",
        "done",
        "files",
        "inject",
        "list",
        "log",
        "rearm",
        "report",
        "show",
        "stats",
        "status",
        "subagents",
    }
)
# Known uv/uvx options that consume the following token. Unknown options fall
# back safely by making that following token the apparent command.
_UV_VALUE_OPTIONS = frozenset(
    {
        "-C",
        "-P",
        "-b",
        "-c",
        "-f",
        "-i",
        "-p",
        "-w",
        "--allow-insecure-host",
        "--build-constraints",
        "--cache-dir",
        "--color",
        "--config-file",
        "--config-setting",
        "--config-settings-package",
        "--constraints",
        "--default-index",
        "--directory",
        "--env-file",
        "--exclude-newer",
        "--exclude-newer-package",
        "--extra",
        "--extra-index-url",
        "--find-links",
        "--fork-strategy",
        "--from",
        "--group",
        "--index",
        "--index-strategy",
        "--index-url",
        "--keyring-provider",
        "--link-mode",
        "--no-binary-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-extra",
        "--no-group",
        "--only-group",
        "--overrides",
        "--package",
        "--prerelease",
        "--project",
        "--python",
        "--python-platform",
        "--refresh-package",
        "--reinstall-package",
        "--resolution",
        "--upgrade-package",
        "--with",
        "--with-editable",
        "--with-requirements",
    }
)
_YIELD_OPTION = "--yield-after-secs"
_TIMEOUT_OPTION = "--timeout"
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)


@dataclass(frozen=True)
class SpawnCommand:
    """A certain-to-finish `meridian spawn` invocation."""

    waits: bool
    # `spawn wait --yield-after-secs`; None means the configured default.
    yield_seconds: float | None = None
    # `spawn wait --timeout` is in minutes and disables yielding.
    timeout_minutes: float | None = None


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


def _command_index(tokens: list[str], start: int = 0) -> int | None:
    index = start
    while index < len(tokens) and _ASSIGNMENT.fullmatch(tokens[index]) is not None:
        index += 1
    return index if index < len(tokens) else None


def _skip_options(tokens: list[str], start: int, value_options: frozenset[str]) -> int | None:
    index = start
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        if option == "--":
            return index + 1 if index + 1 < len(tokens) else None
        name, has_equals, _value = option.partition("=")
        if name in value_options and not has_equals:
            index += 2
            if index > len(tokens):
                return None
        else:
            index += 1
    return index if index < len(tokens) else None


def _meridian_index(tokens: list[str]) -> int | None:
    index = _command_index(tokens)
    if index is None:
        return None
    command = PurePosixPath(tokens[index]).name
    if command == "meridian":
        return index
    if command == "uv":
        if index + 1 >= len(tokens) or tokens[index + 1] != "run":
            return None
        index = _skip_options(tokens, index + 2, _UV_VALUE_OPTIONS)
    elif command == "uvx":
        index = _skip_options(tokens, index + 1, _UV_VALUE_OPTIONS)
    else:
        return None
    if index is None or PurePosixPath(tokens[index]).name != "meridian":
        return None
    return index


def _option_number(args: list[str], option: str) -> tuple[bool, float | None]:
    for index, arg in enumerate(args):
        raw: str | None = None
        if arg == option:
            if index + 1 >= len(args):
                return True, None
            raw = args[index + 1]
        elif arg.startswith(f"{option}="):
            raw = arg.partition("=")[2]
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            return True, None
        return True, value if math.isfinite(value) and value > 0 else None
    return False, None


def _spawn_in(tokens: list[str]) -> SpawnCommand | None:
    start = _meridian_index(tokens)
    if start is None:
        return None
    index = start + 1
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        name, has_equals, _value = option.partition("=")
        if name in _GLOBAL_VALUE_OPTIONS and not has_equals:
            index += 2
            if index > len(tokens):
                return None
        else:
            index += 1
    if index >= len(tokens) or tokens[index] != "spawn":
        return None

    rest = tokens[index + 1 :]
    if rest and rest[0] == "wait":
        timeout_present, timeout_minutes = _option_number(rest[1:], _TIMEOUT_OPTION)
        yield_present, yield_seconds = _option_number(rest[1:], _YIELD_OPTION)
        if (timeout_present and timeout_minutes is None) or (
            yield_present and yield_seconds is None
        ):
            return None
        return SpawnCommand(
            waits=True,
            yield_seconds=yield_seconds,
            timeout_minutes=timeout_minutes,
        )
    if rest and rest[0] in _SPAWN_SUBCOMMANDS:
        return None
    for arg in rest:
        if arg == "--":
            break
        if arg in {"--bg", "--background"} or arg.startswith(
            ("--bg=", "--background=")
        ):
            return None
    return SpawnCommand(waits=False)


def spawn_command(command: str) -> SpawnCommand | None:
    """Read a `meridian spawn` whose completion is certain to end this shell."""

    segments = _segments(command)
    if not segments:
        return None
    if any(
        (index := _command_index(segment)) is not None
        and segment[index] in _LOOP_COMMANDS
        for segment in segments
    ):
        return None
    return _spawn_in(segments[-1])


def expected_wake(
    tasks: Iterable[BackgroundTask],
    *,
    now_ms: int,
    default_yield_seconds: float,
) -> ExpectedWake:
    """Reduce the running work to what it says about the next wake.

    Agents and foreground `meridian spawn` shells end and wake the session; a
    `spawn wait` also returns by a known time. Watchers never count: they may
    stand forever and wake only on events.
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
                seconds = (
                    found.timeout_minutes * 60
                    if found.timeout_minutes is not None
                    else (found.yield_seconds or default_yield_seconds)
                )
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
