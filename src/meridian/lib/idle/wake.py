"""Pure reading of the work a turn left running: will it wake the session, and by when?"""

from __future__ import annotations

import math
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from meridian.lib.cli_args import TOP_LEVEL_VALUE_FLAGS
from meridian.lib.harness.idle_types import BackgroundTask

_LOOP_COMMANDS = frozenset({"while", "until", "for", "select"})
_YIELD_OPTION = "--yield-after-secs"
_TIMEOUT_OPTION = "--timeout"
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_NEWLINE_BOUNDARY = "\0"
_PUNCTUATION = f"();<>|&{_NEWLINE_BOUNDARY}"
_SEGMENT_BOUNDARIES = frozenset(
    {";", "&&", "||", "|", "&", "(", ")", _NEWLINE_BOUNDARY}
)
_REDIRECTIONS = frozenset({">", ">>", "<", ">&", "<&", "&>", ">|"})


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


def _lex_input(command: str) -> str | None:
    """Mark command newlines, strip comments, and remove redirection fd numbers."""

    if _NEWLINE_BOUNDARY in command:
        return None
    chars = list(command)
    quote: str | None = None
    escaped = False
    word_started = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            if char != "\n":
                word_started = True
            index += 1
            continue
        if char == "\\" and quote != "'":
            if command[index + 1 : index + 2] == "\n":
                # The shell removes an unquoted backslash-newline pair before
                # tokenization; it neither ends the command nor starts a word.
                chars[index : index + 2] = "  "
                index += 2
                continue
            escaped = True
            index += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            word_started = True
            index += 1
            continue
        if char == "#" and not word_started:
            # Quotes and backslashes inside a shell comment are inert. Blank
            # them so shlex cannot mistake them for live syntax.
            while index < len(command) and command[index] != "\n":
                chars[index] = " "
                index += 1
            continue
        if char == "\n":
            # Keep the newline so shlex ends a shell comment there, then emit a
            # distinct token that quoted and escaped newlines never produce.
            chars[index] = f"\n{_NEWLINE_BOUNDARY}"
            word_started = False
            index += 1
            continue
        if char.isspace():
            word_started = False
            index += 1
            continue
        if char not in "();<>|&":
            word_started = True
            index += 1
            continue

        if char in "<>" or (char == "&" and command[index : index + 2] == "&>"):
            # POSIX recognizes an IO number only when its digits touch the
            # redirection operator. Blank it so it cannot become a command argument.
            start = index
            while start > 0 and command[start - 1].isdigit():
                start -= 1
            if start < index and (
                start == 0 or command[start - 1].isspace() or command[start - 1] in "();|&"
            ):
                chars[start:index] = " " * (index - start)
        word_started = False
        index += 1
    return "".join(chars)


def _segments(command: str) -> list[list[str]]:
    prepared = _lex_input(command)
    if prepared is None:
        return []
    lexer = shlex.shlex(prepared, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    redirection_target = False
    try:
        for token in lexer:
            if redirection_target:
                if token and all(char in _PUNCTUATION for char in token):
                    return []
                redirection_target = False
            elif token in _REDIRECTIONS:
                redirection_target = True
            elif token in _SEGMENT_BOUNDARIES:
                segments.append([])
            elif token and all(char in _PUNCTUATION for char in token):
                return []
            else:
                segments[-1].append(token)
    except ValueError:  # unbalanced quotes: not a command we can read
        return []
    if redirection_target:
        return []
    return [segment for segment in segments if segment]


def _command_index(tokens: list[str], start: int = 0) -> int | None:
    index = start
    while index < len(tokens) and _ASSIGNMENT.fullmatch(tokens[index]) is not None:
        index += 1
    return index if index < len(tokens) else None


def _meridian_starts(tokens: list[str]) -> list[int]:
    """Indexes where `meridian` may start this segment's command.

    The command word itself, or, behind `uv run` / `uvx`, any later token named
    `meridian`: their options are not parsed, and a candidate that is not
    followed by `spawn` is simply passed over.
    """

    index = _command_index(tokens)
    if index is None:
        return []
    command = PurePosixPath(tokens[index]).name
    if command == "meridian":
        return [index]
    if command == "uvx" or (command == "uv" and tokens[index + 1 : index + 2] == ["run"]):
        return [
            candidate
            for candidate in range(index + 1, len(tokens))
            if PurePosixPath(tokens[candidate]).name == "meridian"
        ]
    return []


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
    for start in _meridian_starts(tokens):
        found = _spawn_after(tokens, start)
        if found is not None:
            return found
    return None


def _spawn_after(tokens: list[str], start: int) -> SpawnCommand | None:
    index = start + 1
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        name, has_equals, _value = option.partition("=")
        if name in TOP_LEVEL_VALUE_FLAGS and not has_equals:
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
    # Creation starts with an option (`spawn -a coder -p ...`); any other word is
    # a management subcommand (`list`, `show`, ...) that does not wake the session.
    if not rest or not rest[0].startswith("-"):
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
