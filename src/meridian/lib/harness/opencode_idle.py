"""OpenCode idle observations and compaction actuation."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Literal, cast
from urllib.parse import quote

import aiohttp

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.idle_types import (
    CompactResult,
    IdleEnvFacts,
    IdleEvent,
    IdleFacts,
    IdleSensorContext,
)

CapturePane = Callable[[str], Awaitable[str | None]]
BackendRequest = Callable[
    [str, str, Mapping[str, object] | None], Awaitable[tuple[int, str]]
]


def _mapping(value: object) -> Mapping[str, object] | None:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else None


def _properties(event: RawHarnessEvent) -> Mapping[str, object]:
    return _mapping(event.payload.get("properties")) or event.payload


def _session_id(event: RawHarnessEvent) -> str | None:
    properties = _properties(event)
    value = properties.get("sessionID") or event.payload.get("sessionID")
    return value if isinstance(value, str) and value else None


def _event_id(event: RawHarnessEvent) -> str | None:
    value = event.payload.get("id")
    return value if isinstance(value, str) and value else None


def _epoch_ms(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value * 1000 if value < 10_000_000_000 else value)


def _user_message(event: RawHarnessEvent) -> tuple[str, float] | None:
    if event.event_type != "message.updated":
        return None
    info = _mapping(_properties(event).get("info"))
    if info is None or info.get("role") != "user":
        return None
    message_id = info.get("id")
    timestamp = _mapping(info.get("time"))
    created_ms = _epoch_ms(timestamp.get("created")) if timestamp is not None else None
    if not isinstance(message_id, str) or not message_id or created_ms is None:
        return None
    return message_id, created_ms


def _message_part(event: RawHarnessEvent) -> tuple[str, str] | None:
    if event.event_type != "message.part.updated":
        return None
    part = _mapping(_properties(event).get("part"))
    if part is None:
        return None
    message_id = part.get("messageID")
    part_type = part.get("type")
    if not isinstance(message_id, str) or not isinstance(part_type, str):
        return None
    return message_id, part_type


def _status(event: RawHarnessEvent) -> str | None:
    if event.event_type != "session.status":
        return None
    status = _mapping(_properties(event).get("status"))
    value = status.get("type") if status is not None else None
    return value if isinstance(value, str) else None


def _draft_from_pane(capture: str) -> Literal["yes", "no"] | None:
    """Read the final OpenCode composer box from a plain tmux pane capture."""

    groups: list[list[str]] = []
    current: list[str] = []
    for line in capture.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("┃"):
            current.append(stripped.removeprefix("┃"))
            continue
        if current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    for box in reversed(groups):
        if len(box) < 4:
            continue
        top, *prompt_rows, footer = box
        if top.strip() or " · " not in footer:
            continue
        return "yes" if any(row.strip() for row in prompt_rows) else "no"
    return None


async def _capture_tmux_pane(pane: str) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "tmux",
            "capture-pane",
            "-p",
            "-t",
            pane,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2.0)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None
    if process.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace")


async def _backend_request(
    ctx: IdleSensorContext,
    method: str,
    path: str,
    payload: Mapping[str, object] | None,
) -> tuple[int, str]:
    endpoint = ctx.connection.observer_endpoint
    if endpoint is None or endpoint.transport != "http":
        raise RuntimeError("OpenCode observer endpoint is unavailable")
    password = endpoint.client_env.get("OPENCODE_PASSWORD")
    auth = aiohttp.BasicAuth("opencode", password) if password is not None else None
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout, auth=auth) as client:
        url = f"{endpoint.url.rstrip('/')}{path}"
        if payload is None:
            async with client.request(method, url) as response:
                return int(response.status), await response.text()
        async with client.request(method, url, json=dict(payload)) as response:
            return int(response.status), await response.text()


class OpenCodeIdleSensor:
    """Translate a managed OpenCode session's raw stream into idle facts."""

    def __init__(
        self,
        ctx: IdleSensorContext,
        *,
        now: Callable[[], float] = time.time,
        capture_pane: CapturePane = _capture_tmux_pane,
        request: BackendRequest | None = None,
    ) -> None:
        self._ctx = ctx
        self._now = now
        self._capture_pane = capture_pane
        if request is None:

            async def request_backend(
                method: str,
                path: str,
                payload: Mapping[str, object] | None,
            ) -> tuple[int, str]:
                return await _backend_request(ctx, method, path, payload)

            self._request: BackendRequest = request_backend
        else:
            self._request = request
        self._events: asyncio.Queue[IdleEvent] = asyncio.Queue()
        self._busy = False
        self._saw_busy = False
        self._last_anchor_ms = now() * 1000
        self._seen_user_messages: set[str] = set()
        self._pending_user_messages: set[str] = set()
        self._compaction_messages: set[str] = set()

    def _emit(
        self,
        kind: Literal["turn_end", "user_prompt", "busy", "idle"],
        **kwargs: object,
    ) -> None:
        self._events.put_nowait(
            IdleEvent(
                kind=kind,
                harness_session_id=self._ctx.harness_session_id,
                turn_id=cast("str | None", kwargs.get("turn_id")),
                timestamp=self._now(),
                message_id=cast("str | None", kwargs.get("message_id")),
            )
        )

    def _classify_pending_user_messages(self) -> None:
        for message_id in tuple(self._pending_user_messages):
            self._pending_user_messages.discard(message_id)
            self._seen_user_messages.add(message_id)
            if message_id not in self._compaction_messages:
                self._emit("user_prompt", message_id=message_id)
            self._compaction_messages.discard(message_id)
        self._compaction_messages.clear()

    def on_raw_event(self, event: RawHarnessEvent) -> None:
        if event.harness_id != self._ctx.harness_id.value:
            return
        session_id = _session_id(event)
        if session_id != self._ctx.harness_session_id:
            return

        part = _message_part(event)
        if part is not None:
            message_id, part_type = part
            if part_type == "compaction":
                self._compaction_messages.add(message_id)

        user_message = _user_message(event)
        if user_message is not None:
            message_id, created_ms = user_message
            if (
                message_id not in self._seen_user_messages
                and message_id not in self._pending_user_messages
            ):
                if created_ms > self._last_anchor_ms:
                    self._pending_user_messages.add(message_id)
                else:
                    self._seen_user_messages.add(message_id)

        status = _status(event)
        if status == "busy":
            self._classify_pending_user_messages()
            self._saw_busy = True
            if not self._busy:
                self._busy = True
                self._emit("busy")
            return
        if status == "idle":
            self._busy = False
            return
        if event.event_type != "session.idle" or not self._saw_busy or self._busy:
            return

        self._saw_busy = False
        self._last_anchor_ms = self._now() * 1000
        self._emit("turn_end", turn_id=_event_id(event))

    async def events(self) -> AsyncIterator[IdleEvent]:
        while True:
            yield await self._events.get()

    async def facts(self) -> IdleFacts:
        draft: Literal["yes", "no", "unknown"] = "unknown"
        pane = self._ctx.tmux_pane
        if pane:
            capture = await self._capture_pane(pane)
            if capture is not None:
                draft = _draft_from_pane(capture) or "unknown"
        return IdleFacts(
            draft=draft,
            busy=self._busy,
            agents_running=0,
            context_tokens=None,
            harness_autocompact_off=bool(
                idle_env_facts(self._ctx.env).harness_autocompact_off
            ),
        )

    async def compact(self) -> CompactResult:
        if not self._ctx.tui_alive():
            return CompactResult("vetoed", "tui-exited")
        session_id = quote(self._ctx.harness_session_id, safe="")
        session_path = f"/session/{session_id}"
        try:
            status, body = await self._request("GET", session_path, None)
            if status != 200:
                return CompactResult("failed", body)
            session = _mapping(json.loads(body))
            model = _mapping(session.get("model")) if session is not None else None
            provider = model.get("providerID") if model is not None else None
            model_id = model.get("id") if model is not None else None
            if (
                not isinstance(provider, str)
                or not provider
                or not isinstance(model_id, str)
                or not model_id
            ):
                return CompactResult("failed", "session model is unavailable")
            if not self._ctx.tui_alive():
                return CompactResult("vetoed", "tui-exited")
            status, body = await self._request(
                "POST",
                f"{session_path}/summarize",
                {"providerID": provider, "modelID": model_id},
            )
        except (OSError, RuntimeError, aiohttp.ClientError, json.JSONDecodeError) as exc:
            return CompactResult("failed", str(exc))
        if status == 200 and body.strip() == "true":
            return CompactResult("ok")
        return CompactResult("failed", body)


def primary_idle_sensor(ctx: IdleSensorContext) -> OpenCodeIdleSensor:
    """Create the sensor hosted by the managed-primary attach launcher."""

    return OpenCodeIdleSensor(ctx)


def idle_env_facts(env: Mapping[str, str]) -> IdleEnvFacts:
    """Read OpenCode facts that are visible only in the process environment."""

    return IdleEnvFacts(
        harness_autocompact_off=bool(env.get("OPENCODE_DISABLE_AUTOCOMPACT")),
        cache_retention=None,
    )


__all__ = ["OpenCodeIdleSensor", "idle_env_facts", "primary_idle_sensor"]
