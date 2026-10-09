"""Codex idle observations and tmux compaction actuator."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import subprocess
import time
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.idle_types import (
    CompactResult,
    IdleEvent,
    IdleFacts,
    IdleSensor,
    IdleSensorContext,
)
from meridian.lib.platform import get_home_path

_EMPTY_PROMPT = "Ask Codex to do anything"
_PROMPT_MARKER = "\u203a"
_COMPACT_COMMAND = "/compact"
_COMPACT_MARKER = "Context compacted"
IDLE_NOTIFY_COMMAND = ("meridian", "idle", "event", "--harness", "codex")


@dataclass(frozen=True)
class PinnedIdleSession:
    """Minimum persisted fact needed to accept one Codex notify event."""

    last_input_count: int | None


class TmuxClient(Protocol):
    """Small async boundary around the tmux commands used by the actuator."""

    async def capture(self, pane: str) -> str | None: ...

    async def send_literal(self, pane: str, text: str) -> bool: ...

    async def send_keys(self, pane: str, *keys: str) -> bool: ...


async def _run_tmux(*args: str, capture: bool = False) -> tuple[bool, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
    except (OSError, ValueError):
        return False, ""
    if process.returncode != 0:
        return False, ""
    output = stdout.decode("utf-8", errors="replace") if capture else ""
    return True, output


class _Tmux:
    async def capture(self, pane: str) -> str | None:
        ok, output = await _run_tmux("capture-pane", "-p", "-t", pane, capture=True)
        return output if ok else None

    async def send_literal(self, pane: str, text: str) -> bool:
        ok, _ = await _run_tmux("send-keys", "-t", pane, "-l", text)
        return ok

    async def send_keys(self, pane: str, *keys: str) -> bool:
        ok, _ = await _run_tmux("send-keys", "-t", pane, *keys)
        return ok


def _prompt_text(capture: str) -> str | None:
    for line in reversed(capture.splitlines()):
        normalized = line.lstrip()
        if normalized.startswith(_PROMPT_MARKER):
            return normalized.removeprefix(_PROMPT_MARKER).strip()
    return None


def _prompt_is_empty(capture: str) -> bool:
    prompt = _prompt_text(capture)
    return prompt in {"", _EMPTY_PROMPT}


def pane_facts(capture: str) -> IdleFacts:
    """Map one Codex pane capture to the facts visible in its TUI."""

    prompt = _prompt_text(capture)
    if prompt is None:
        draft: Literal["yes", "no", "unknown"] = "unknown"
    elif prompt in {"", _EMPTY_PROMPT}:
        draft = "no"
    else:
        draft = "yes"
    return IdleFacts(
        draft=draft,
        busy="Working (" in capture,
        agents_running=0,
        context_tokens=None,
        harness_autocompact_off=False,
    )


def _read_pinned_session(session: str) -> PinnedIdleSession | None:
    # The service is the adapter-facing read seam. The harness module never
    # derives or opens an idle-store path itself.
    from meridian.lib.idle.service import IdleService

    states = IdleService(env=os.environ).status(harness="codex", session=session)
    if not states:
        return None
    return PinnedIdleSession(last_input_count=states[0].last_input_count)


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


def _chain_user_notify(payload: str, env: Mapping[str, str]) -> None:
    command = _read_user_notify(env)
    if command is not None and command[: len(IDLE_NOTIFY_COMMAND)] != IDLE_NOTIFY_COMMAND:
        # The CLI applies the parsed event after this function returns. Process
        # exit therefore runs the user's handler after Meridian's own work.
        atexit.register(_run_user_notify, command, payload)


def parse_idle_event(
    payload: str,
    *,
    session_reader: Callable[[str], PinnedIdleSession | None] = _read_pinned_session,
    chain_user_notify: bool = True,
) -> IdleEvent | None:
    """Parse one Codex ``notify`` argv payload pinned to an active primary."""

    if chain_user_notify:
        _chain_user_notify(payload, os.environ)
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
    input_count = len(cast("list[object]", input_messages))
    previous_count = pinned.last_input_count or 0
    return IdleEvent(
        kind="turn_end",
        harness_session_id=thread_id,
        turn_id=turn_id,
        timestamp=time.time(),
        implies_return=input_count > previous_count,
        input_count=input_count,
    )


class CodexIdleSensor:
    """Pane-backed facts and typed-then-verified compaction for Codex."""

    external_events = True

    def __init__(
        self,
        ctx: IdleSensorContext,
        *,
        tmux: TmuxClient | None = None,
        compact_timeout_seconds: float = 60.0,
        compact_poll_seconds: float = 0.5,
        verification_delay_seconds: float = 0.2,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._ctx = ctx
        self._tmux = tmux or _Tmux()
        self._compact_timeout_seconds = compact_timeout_seconds
        self._compact_poll_seconds = compact_poll_seconds
        self._verification_delay_seconds = verification_delay_seconds
        self._monotonic = monotonic
        self._sleep = sleep

    def on_raw_event(self, event: RawHarnessEvent) -> None:
        _ = event

    async def events(self) -> AsyncIterator[IdleEvent]:
        # Codex's observer stream closes when its TUI takes over the endpoint.
        if False:
            yield IdleEvent("idle", self._ctx.harness_session_id, None, time.time())

    async def facts(self) -> IdleFacts:
        pane = self._ctx.tmux_pane
        if pane is None or not self._ctx.tui_alive():
            return pane_facts("")
        capture = await self._tmux.capture(pane)
        return pane_facts(capture or "")

    async def _erase_compact_text(self, pane: str) -> None:
        if self._ctx.tui_alive():
            await self._tmux.send_keys(pane, *("BSpace",) * len(_COMPACT_COMMAND))

    async def compact(self) -> CompactResult:
        pane = self._ctx.tmux_pane
        if pane is None or not self._ctx.tui_alive():
            return CompactResult("vetoed", "tui-unavailable")

        before = await self._tmux.capture(pane)
        if before is None or not _prompt_is_empty(before):
            return CompactResult("vetoed", "prompt-not-empty")
        prior_markers = before.count(_COMPACT_MARKER)

        if not await self._tmux.send_literal(pane, _COMPACT_COMMAND):
            return CompactResult("vetoed", "type-failed")
        if not self._ctx.tui_alive():
            return CompactResult("vetoed", "tui-exited-before-submit")

        # send-keys returning only means tmux accepted the input. Give the TUI
        # one render tick before verifying the editor contents.
        await self._sleep(self._verification_delay_seconds)
        verified = await self._tmux.capture(pane)
        if verified is None or _prompt_text(verified) != _COMPACT_COMMAND:
            await self._erase_compact_text(pane)
            return CompactResult("vetoed", "typed-text-changed")
        if not await self._tmux.send_keys(pane, "Enter"):
            await self._erase_compact_text(pane)
            return CompactResult("vetoed", "submit-failed")

        deadline = self._monotonic() + self._compact_timeout_seconds
        while self._monotonic() < deadline:
            if not self._ctx.tui_alive():
                return CompactResult("failed", "tui-exited")
            capture = await self._tmux.capture(pane)
            if capture is not None:
                if capture.count(_COMPACT_MARKER) > prior_markers:
                    return CompactResult("ok")
                if _prompt_is_empty(capture):
                    return CompactResult("ok")
            await self._sleep(self._compact_poll_seconds)
        return CompactResult("failed", "timeout")


def primary_idle_sensor(ctx: IdleSensorContext) -> IdleSensor:
    """Build the managed-primary Codex sensor."""

    return CodexIdleSensor(ctx)


__all__ = [
    "IDLE_NOTIFY_COMMAND",
    "CodexIdleSensor",
    "PinnedIdleSession",
    "pane_facts",
    "parse_idle_event",
    "primary_idle_sensor",
]
