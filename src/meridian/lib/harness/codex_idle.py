"""Codex idle observations and tmux compaction actuator."""

from __future__ import annotations

import asyncio
import atexit
import json
import subprocess
import time
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast

from meridian.lib.core.native_identity import NativeIdentityError
from meridian.lib.harness.codex_bootstrap import is_bootstrap_turn_prompt
from meridian.lib.harness.codex_rollout import (
    find_rollout,
    latest_context_tokens,
    resolve_codex_home,
)
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.idle_types import (
    CompactCosts,
    CompactResult,
    IdleEvent,
    IdleFacts,
    IdleSensor,
    IdleSensorContext,
    NativeIdleHook,
    PinnedIdleSession,
)
from meridian.lib.platform import get_home_path

_PROMPT_MARKER = "\u203a"
_COMPACT_COMMAND = "/compact"
_CLEAR_PRIMER = "ix"
IDLE_NOTIFY_COMMAND = ("meridian", "idle", "event", "--harness", "codex")


def compact_costs(
    *, ttl_seconds: int | None
) -> CompactCosts | None:
    """Return measured remote-compaction economics for Codex's OpenAI route."""

    _ = ttl_seconds
    # 160 rollout compactions put cached input at median 0.114*C and summary
    # output at 4.5k tokens. R=3.5k is the cross-session post-minus-prefix
    # median; GPT-5.x output is priced at roughly 8x input.
    return CompactCosts(
        w=1.0,
        c=0.114,
        u=0,
        o=8.0,
        summary_tokens=4_500,
        residue_tokens=3_500,
    )


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


def _composer_region(capture: str) -> tuple[tuple[str, ...], int, int, bool] | None:
    """Return the visible composer rows, or ``None`` for any other surface."""

    lines = tuple(capture.splitlines())
    if len(lines) < 6:
        return None

    status_row = lines[-2]
    footer_text = lines[-1].strip()
    footer_says_empty = footer_text.endswith(" for shortcuts")
    if (
        not status_row.startswith("  ")
        or not status_row.strip()
        or (bool(footer_text) and not footer_says_empty)
        or lines[-3].strip()
    ):
        return None

    separator_index = len(lines) - 3
    prompt_index = next(
        (
            index
            for index in range(separator_index - 1, -1, -1)
            if lines[index].startswith(_PROMPT_MARKER)
        ),
        -1,
    )
    if (
        prompt_index < 2
        or any(lines[index].strip() for index in range(prompt_index - 2, prompt_index))
    ):
        return None

    continuation_rows = lines[prompt_index + 1 : separator_index]
    if (
        (footer_says_empty and bool(continuation_rows))
        or any(bool(row) and not row.startswith("  ") for row in continuation_rows)
    ):
        return None
    return lines, prompt_index, separator_index, footer_says_empty


def _composer_text(capture: str) -> str | None:
    """Return visible editor text only when the normal composer owns focus."""

    region = _composer_region(capture)
    if region is None:
        return None
    lines, prompt_index, separator_index, footer_says_empty = region
    if footer_says_empty:
        return ""

    first_row = lines[prompt_index].removeprefix(_PROMPT_MARKER)
    if first_row.startswith(" "):
        first_row = first_row[1:]
    continuation = (
        row.removeprefix("  ") for row in lines[prompt_index + 1 : separator_index]
    )
    return "".join((first_row, *continuation))


def _composer_is_empty(capture: str) -> bool:
    """Whether the focused composer shows no editable or continuation rows."""

    region = _composer_region(capture)
    if region is None:
        return False
    lines, prompt_index, separator_index, footer_says_empty = region
    if separator_index != prompt_index + 1:
        return False
    if footer_says_empty:
        return True
    return lines[prompt_index].removeprefix(_PROMPT_MARKER).strip() == ""


def _find_session_rollout(session_id: str, env: Mapping[str, str]) -> Path | None:
    sessions_root = resolve_codex_home(env) / "sessions"
    try:
        return find_rollout(sessions_root, session_id)
    except (OSError, NativeIdentityError):
        return None


def _session_rollout(ctx: IdleSensorContext) -> Path | None:
    return _find_session_rollout(ctx.harness_session_id, ctx.env)


def _has_appended_compaction(path: Path, offset: int) -> bool:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            appended = handle.read()
    except OSError:
        return False

    for line in appended.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            continue
        try:
            parsed: object = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        record = cast("dict[str, object]", parsed)
        if record.get("type") == "compacted" and isinstance(record.get("payload"), dict):
            return True
    return False


def pane_facts(capture: str) -> IdleFacts:
    """Map one Codex pane capture to the facts visible in its TUI."""

    return IdleFacts(
        busy="Working (" in capture,
        agents_running=0,
        context_tokens=None,
        harness_autocompact_off=False,
    )


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
    last_assistant = raw.get("last-assistant-message")
    rollout = _find_session_rollout(thread_id, env)
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
        context_tokens=latest_context_tokens(rollout) if rollout is not None else None,
    )


CODEX_NATIVE_IDLE_HOOK = NativeIdleHook(
    observe=parse_idle_event,
    delegate=_delegate_user_notify,
)


class CodexIdleSensor:
    """Pane-backed facts and typed-then-verified compaction for Codex."""

    external_events = True

    def __init__(
        self,
        ctx: IdleSensorContext,
        *,
        tmux: TmuxClient | None = None,
        compact_timeout_seconds: float = 300.0,
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
            yield

    async def facts(self) -> IdleFacts:
        rollout = _session_rollout(self._ctx)
        context_tokens = latest_context_tokens(rollout) if rollout is not None else None
        pane = self._ctx.tmux_pane
        if pane is None or not self._ctx.tui_alive():
            return replace(pane_facts(""), context_tokens=context_tokens)
        capture = await self._tmux.capture(pane)
        return replace(pane_facts(capture or ""), context_tokens=context_tokens)

    async def _erase_typed_text(self, pane: str, text: str) -> None:
        await self._tmux.send_keys(pane, *("BSpace",) * len(text))

    async def compact(self) -> CompactResult:
        pane = self._ctx.tmux_pane
        if pane is None or not self._ctx.tui_alive():
            return CompactResult("vetoed", "tui-unavailable")

        rollout = _session_rollout(self._ctx)
        if rollout is None:
            return CompactResult("vetoed", "rollout-not-found")
        try:
            rollout_offset = rollout.stat().st_size
        except OSError:
            return CompactResult("vetoed", "rollout-unavailable")

        # Codex 0.162.1 clears the whole composer with Ctrl+C only when it is
        # nonempty. Its normal focused layout ends with the prompt, a blank
        # composer margin, status, and footer rows. Suggestion popups consume
        # the two-row gap above that prompt; modals and overlays replace the
        # footer. Only a screen matching the complete layout may reach Ctrl+C.
        before = await self._tmux.capture(pane)
        if before is not None and "Working (" in before:
            return CompactResult("vetoed", "busy")
        before_text = _composer_text(before or "")
        if before_text is None:
            return CompactResult("vetoed", "composer-not-focused")
        if not self._ctx.tui_alive():
            return CompactResult("vetoed", "tui-exited-before-clear")
        if not await self._tmux.send_literal(pane, _CLEAR_PRIMER):
            return CompactResult("vetoed", "clear-prime-failed")

        await self._sleep(self._verification_delay_seconds)
        primed = await self._tmux.capture(pane)
        if primed is None or _composer_text(primed) != before_text + _CLEAR_PRIMER:
            await self._erase_typed_text(pane, _CLEAR_PRIMER)
            return CompactResult("vetoed", "clear-primer-not-visible")
        if "Working (" in primed:
            await self._erase_typed_text(pane, _CLEAR_PRIMER)
            return CompactResult("vetoed", "busy")
        if not self._ctx.tui_alive():
            await self._erase_typed_text(pane, _CLEAR_PRIMER)
            return CompactResult("vetoed", "tui-exited-before-clear")
        if not await self._tmux.send_keys(pane, "C-c"):
            return CompactResult("vetoed", "clear-failed")

        await self._sleep(self._verification_delay_seconds)
        cleared = await self._tmux.capture(pane)
        if not _composer_is_empty(cleared or ""):
            return CompactResult("vetoed", "clear-not-confirmed")
        if not self._ctx.tui_alive():
            return CompactResult("vetoed", "tui-exited-before-submit")
        if not await self._tmux.send_literal(pane, _COMPACT_COMMAND):
            return CompactResult("vetoed", "type-failed")
        if not self._ctx.tui_alive():
            return CompactResult("vetoed", "tui-exited-before-submit")

        # send-keys returning only means tmux accepted the input. Give the TUI
        # one render tick before verifying the editor contents.
        await self._sleep(self._verification_delay_seconds)
        verified = await self._tmux.capture(pane)
        if verified is None or _prompt_text(verified) != _COMPACT_COMMAND:
            return CompactResult("vetoed", "typed-text-changed")
        if not self._ctx.tui_alive():
            return CompactResult("vetoed", "tui-exited-before-submit")
        if not await self._tmux.send_keys(pane, "Enter"):
            await self._erase_typed_text(pane, _COMPACT_COMMAND)
            return CompactResult("vetoed", "submit-failed")

        deadline = self._monotonic() + self._compact_timeout_seconds
        while self._monotonic() < deadline:
            if not self._ctx.tui_alive():
                return CompactResult("failed", "tui-exited")
            if _has_appended_compaction(rollout, rollout_offset):
                return CompactResult("ok")
            await self._sleep(self._compact_poll_seconds)
        return CompactResult("failed", "timeout")


def primary_idle_sensor(ctx: IdleSensorContext) -> IdleSensor:
    """Build the managed-primary Codex sensor."""

    return CodexIdleSensor(ctx)


__all__ = [
    "CODEX_NATIVE_IDLE_HOOK",
    "IDLE_NOTIFY_COMMAND",
    "CodexIdleSensor",
    "compact_costs",
    "pane_facts",
    "parse_idle_event",
    "primary_idle_sensor",
]
