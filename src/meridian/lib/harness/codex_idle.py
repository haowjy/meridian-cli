"""Codex idle observations: native notify parsing and pane-backed busy state."""

from __future__ import annotations

import asyncio
import atexit
import json
import subprocess
import tomllib
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol, cast

from meridian.lib.harness.codex_bootstrap import is_bootstrap_turn_prompt
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.idle_types import (
    IdleEvent,
    IdleSensor,
    IdleSensorContext,
    NativeIdleHook,
    PinnedIdleSession,
)
from meridian.lib.platform import get_home_path

IDLE_NOTIFY_COMMAND = ("meridian", "idle", "event", "--harness", "codex")


class TmuxClient(Protocol):
    """Small async boundary around the tmux pane capture used for busy state."""

    async def capture(self, pane: str) -> str | None: ...


class _Tmux:
    async def capture(self, pane: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "tmux",
                "capture-pane",
                "-p",
                "-t",
                pane,
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
        except (OSError, ValueError):
            return None
        if process.returncode != 0:
            return None
        return stdout.decode("utf-8", errors="replace")


def pane_busy(capture: str) -> bool:
    """Whether one Codex pane capture shows a turn in progress."""

    return "Working (" in capture


def _codex_config_path(env: Mapping[str, str]) -> Path:
    configured = env.get("CODEX_HOME", "").strip()
    home = Path(configured).expanduser() if configured else get_home_path() / ".codex"
    return home / "config.toml"


def _read_user_notify(env: Mapping[str, str]) -> tuple[str, ...] | None:
    try:
        with _codex_config_path(env).open("rb") as handle:
            config = cast("dict[str, object]", tomllib.load(handle))
            value = config.get("notify")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(value, list) or not value:
        return None
    arguments = cast("list[object]", value)
    if not all(isinstance(argument, str) and argument for argument in arguments):
        return None
    return tuple(cast("list[str]", arguments))


def _run_user_notify(command: tuple[str, ...], payload: str) -> None:
    with suppress(OSError):
        subprocess.run(
            (*command, payload),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _delegate_user_notify(payload: str, env: Mapping[str, str]) -> None:
    """Queue the user's Codex notify command to run after Meridian exits."""

    command = _read_user_notify(env)
    if command is not None and command[: len(IDLE_NOTIFY_COMMAND)] != IDLE_NOTIFY_COMMAND:
        # The CLI has already applied the event. Process exit keeps the user's
        # handler after Meridian's JSON response and remaining teardown.
        atexit.register(_run_user_notify, command, payload)


def parse_idle_event(
    payload: str,
    session_reader: Callable[[str], PinnedIdleSession | None],
    env: Mapping[str, str],
) -> IdleEvent | None:
    """Parse one Codex ``notify`` argv payload pinned to an active primary."""

    try:
        parsed: object = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    raw = cast("dict[str, object]", parsed)
    if raw.get("type") != "agent-turn-complete":
        return None
    thread_id = raw.get("thread-id")
    turn_id = raw.get("turn-id")
    input_messages = raw.get("input-messages")
    if (
        not isinstance(thread_id, str)
        or not thread_id.strip()
        or not isinstance(turn_id, str)
        or not turn_id.strip()
        or not isinstance(input_messages, list)
    ):
        return None

    pinned = session_reader(thread_id)
    if pinned is None:
        return None
    inputs = cast("list[object]", input_messages)
    input_count = len(inputs)
    previous_count = pinned.last_input_count or 0
    last_input = inputs[-1] if inputs else None
    if is_bootstrap_turn_prompt(last_input):
        return None
    _ = env
    last_assistant = raw.get("last-assistant-message")
    return IdleEvent(
        kind="turn_end",
        harness_session_id=thread_id,
        turn_id=turn_id,
        implies_return=input_count > previous_count,
        input_count=input_count,
        last_user_text=last_input if isinstance(last_input, str) else None,
        last_assistant_text=(
            last_assistant if isinstance(last_assistant, str) else None
        ),
    )


CODEX_NATIVE_IDLE_HOOK = NativeIdleHook(
    observe=parse_idle_event,
    delegate=_delegate_user_notify,
)


class CodexIdleSensor:
    """Pane-backed busy state for a managed Codex primary.

    Turn ends arrive through the native ``notify`` hook, not this sensor.
    """

    external_events = True

    def __init__(
        self,
        ctx: IdleSensorContext,
        *,
        tmux: TmuxClient | None = None,
    ) -> None:
        self._ctx = ctx
        self._tmux = tmux or _Tmux()

    def on_raw_event(self, event: RawHarnessEvent) -> None:
        _ = event

    async def events(self) -> AsyncIterator[IdleEvent]:
        # Codex's observer stream closes when its TUI takes over the endpoint.
        if False:
            yield

    async def busy(self) -> bool:
        pane = self._ctx.tmux_pane
        if pane is None or not self._ctx.tui_alive():
            return False
        capture = await self._tmux.capture(pane)
        return pane_busy(capture or "")


def primary_idle_sensor(ctx: IdleSensorContext) -> IdleSensor:
    """Build the managed-primary Codex sensor."""

    return CodexIdleSensor(ctx)


__all__ = [
    "CODEX_NATIVE_IDLE_HOOK",
    "IDLE_NOTIFY_COMMAND",
    "CodexIdleSensor",
    "pane_busy",
    "parse_idle_event",
    "primary_idle_sensor",
]
