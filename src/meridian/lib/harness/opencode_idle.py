"""OpenCode idle observations and compaction actuation."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

import aiohttp

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.idle_types import (
    CacheTtl,
    CompactResult,
    IdleEvent,
    IdleFacts,
    IdleSensor,
    IdleSensorContext,
)
from meridian.lib.harness.opencode_backend import OpenCodeVersion, resolve_opencode_version

CapturePane = Callable[[str], Awaitable[str | None]]
BackendRequest = Callable[[str, str, Mapping[str, object] | None], Awaitable[tuple[int, str]]]

_GET_TIMEOUT_SECONDS = 10.0
_SUMMARIZE_TIMEOUT_SECONDS = 300.0

logger = logging.getLogger(__name__)

# D5/D7: the backend TTL is unknown; mirror the Codex harness's estimate.
_PROVIDER_TTLS = {"openai": CacheTtl(1800, estimated=True)}


def detect_ttl(
    *,
    session_id: str,
    native_store: Path | None,
    provider: str | None,
    env: Mapping[str, str],
) -> CacheTtl | None:
    """Resolve OpenCode cache retention from its current model provider."""

    _ = session_id, native_store, env
    return _PROVIDER_TTLS.get((provider or "").strip().lower())


def _mapping(value: object) -> Mapping[str, object] | None:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else None


def _session_id(event: RawHarnessEvent) -> str | None:
    value = event.payload.get("sessionID")
    return value if isinstance(value, str) and value else None


def _event_id(event: RawHarnessEvent) -> str | None:
    value = event.payload.get("id")
    return value if isinstance(value, str) and value else None


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


def _text_content(value: object) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    if not isinstance(value, list):
        return None
    text: list[str] = []
    for raw_part in cast("list[object]", value):
        part = _mapping(raw_part)
        if part is None or part.get("type") != "text":
            continue
        if part.get("synthetic") or part.get("ignored"):
            continue
        part_text = part.get("text")
        if isinstance(part_text, str) and part_text.strip():
            text.append(part_text)
    return " ".join(text) or None


def _message_excerpts(payload: object) -> tuple[str | None, str | None]:
    """Extract the last user and assistant text parts from a messages response."""

    if not isinstance(payload, list):
        return None, None
    user: str | None = None
    assistant: str | None = None
    for raw_message in cast("list[object]", payload):
        message = _mapping(raw_message)
        if message is None:
            continue
        info = _mapping(message.get("info"))
        role = info.get("role") if info is not None else message.get("role") or message.get("type")
        text = (
            _text_content(message.get("parts"))
            or _text_content(message.get("content"))
            or _text_content(message.get("text"))
        )
        if text is None:
            continue
        if role == "user":
            user = text
        elif role == "assistant":
            assistant = text
    return user, assistant


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


_V2_TERMINAL_EVENTS = frozenset(
    {
        "session.execution.succeeded",
        "session.execution.failed",
        "session.execution.interrupted",
    }
)


def _v2_inbox_item(event: RawHarnessEvent) -> tuple[str, str] | None:
    if event.event_type != "session.inbox.enqueued":
        return None
    inbox_id = event.payload.get("inboxID")
    item = _mapping(event.payload.get("item"))
    item_type = item.get("type") if item is not None else None
    if not isinstance(inbox_id, str) or not isinstance(item_type, str):
        return None
    return inbox_id, item_type


def _v2_delivered_inbox_id(event: RawHarnessEvent) -> str | None:
    if event.event_type != "session.inbox.delivered":
        return None
    inbox_id = event.payload.get("inboxID")
    return inbox_id if isinstance(inbox_id, str) and inbox_id else None


def _v2_compaction_input_id(event: RawHarnessEvent) -> str | None:
    if event.event_type != "session.compaction.started":
        return None
    value = event.payload.get("inputID")
    return value if isinstance(value, str) and value else None


def _v2_response_data(payload: object) -> Mapping[str, object] | None:
    response = _mapping(payload)
    if response is None:
        return None
    return _mapping(response.get("data"))


def _v2_session_model(payload: object) -> tuple[str, str] | None:
    session = _v2_response_data(payload)
    model = _mapping(session.get("model")) if session is not None else None
    provider = model.get("providerID") if model is not None else None
    model_id = model.get("id") if model is not None else None
    if (
        not isinstance(provider, str)
        or not provider
        or not isinstance(model_id, str)
        or not model_id
    ):
        return None
    return provider, model_id


def _v2_message_excerpts(payload: object) -> tuple[str | None, str | None]:
    response = _mapping(payload)
    messages = response.get("data") if response is not None else None
    if not isinstance(messages, list):
        return None, None
    # The V2 request explicitly asks for newest-first results. The shared
    # extractor retains the last text for each role, so restore timeline order.
    return _message_excerpts(list(reversed(cast("list[object]", messages))))


class OpenCodeV2IdleSensor:
    """Translate OpenCode 2.x execution and inbox events into idle facts."""

    def __init__(
        self,
        ctx: IdleSensorContext,
        *,
        capture_pane: CapturePane = _capture_tmux_pane,
        request: BackendRequest | None = None,
        compact_timeout_seconds: float = _SUMMARIZE_TIMEOUT_SECONDS,
    ) -> None:
        self._ctx = ctx
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
        self._compact_timeout_seconds = compact_timeout_seconds
        self._seen_user_inbox_ids: set[str] = set()
        self._pending_user_inbox_ids: set[str] = set()
        self._event_sequence = 0
        self._compaction_started_at: dict[str, int] = {}
        self._last_compaction_ended_at: int | None = None
        self._last_terminal: tuple[int, str] | None = None
        self._compacting = False
        self._compact_expected_id: str | None = None
        self._compact_waiter: asyncio.Future[tuple[bool, str | None]] | None = None

    def _emit(
        self,
        kind: Literal["turn_end", "user_prompt"],
        *,
        turn_id: str | None = None,
    ) -> None:
        self._events.put_nowait(
            IdleEvent(
                kind=kind,
                harness_session_id=self._ctx.harness_session_id,
                turn_id=turn_id,
            )
        )

    async def events(self) -> AsyncIterator[IdleEvent]:
        while True:
            event = await self._events.get()
            if event.kind == "turn_end":
                provider, (user, assistant) = await asyncio.gather(
                    self._fetch_provider(),
                    self._fetch_message_excerpts(),
                )
                event = replace(
                    event,
                    last_user_text=user,
                    last_assistant_text=assistant,
                    provider=provider,
                )
            yield event

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
            harness_autocompact_off=False,
        )

    def _maybe_finish_compaction(self) -> None:
        waiter = self._compact_waiter
        expected_id = self._compact_expected_id
        if waiter is None or waiter.done() or expected_id is None:
            return
        started_at = self._compaction_started_at.get(expected_id)
        ended_at = self._last_compaction_ended_at
        terminal = self._last_terminal
        if started_at is None or terminal is None:
            return
        terminal_at, terminal_type = terminal
        if terminal_at <= started_at:
            return
        if terminal_type != "session.execution.succeeded":
            self._compacting = False
            waiter.set_result((False, terminal_type))
            return
        if ended_at is not None and started_at < ended_at < terminal_at:
            self._compacting = False
            waiter.set_result((True, None))

    def on_raw_event(self, event: RawHarnessEvent) -> None:
        if event.harness_id != self._ctx.harness_id.value:
            return
        if _session_id(event) != self._ctx.harness_session_id:
            return

        self._event_sequence += 1
        inbox_item = _v2_inbox_item(event)
        if inbox_item is not None:
            inbox_id, item_type = inbox_item
            if (
                item_type == "user"
                and inbox_id not in self._seen_user_inbox_ids
            ):
                self._seen_user_inbox_ids.add(inbox_id)
                self._pending_user_inbox_ids.add(inbox_id)
                # Enqueue is the earliest reliable proof of a real user return.
                # A steer-delivered prompt may not start a new execution.
                self._emit("user_prompt")

        cancelled_id = event.payload.get("inboxID")
        if event.event_type == "session.inbox.cancelled" and isinstance(cancelled_id, str):
            self._pending_user_inbox_ids.discard(cancelled_id)

        delivered_id = _v2_delivered_inbox_id(event)
        if delivered_id in self._pending_user_inbox_ids:
            self._pending_user_inbox_ids.remove(delivered_id)
            # Queue delivery can begin after another execution armed an idle
            # stretch. Close it unless Meridian initiated this execution.
            if not self._compacting:
                self._emit("user_prompt")

        if event.event_type == "session.execution.started":
            self._busy = True
            return

        compaction_input_id = _v2_compaction_input_id(event)
        if compaction_input_id is not None:
            self._compaction_started_at[compaction_input_id] = self._event_sequence
            self._maybe_finish_compaction()
            return

        if event.event_type == "session.compaction.ended":
            self._last_compaction_ended_at = self._event_sequence
            self._maybe_finish_compaction()
            return

        if event.event_type not in _V2_TERMINAL_EVENTS:
            return

        self._busy = False
        self._last_terminal = (self._event_sequence, event.event_type)
        self._maybe_finish_compaction()
        self._emit("turn_end", turn_id=_event_id(event))

    async def _fetch_message_excerpts(self) -> tuple[str | None, str | None]:
        session_id = quote(self._ctx.harness_session_id, safe="")
        try:
            status, body = await asyncio.wait_for(
                self._request(
                    "GET",
                    f"/api/session/{session_id}/message?limit=20&order=desc",
                    None,
                ),
                timeout=_GET_TIMEOUT_SECONDS,
            )
            if status != 200:
                return None, None
            return _v2_message_excerpts(json.loads(body))
        except (TimeoutError, OSError, RuntimeError, aiohttp.ClientError, json.JSONDecodeError):
            return None, None

    async def _fetch_provider(self) -> str | None:
        session_id = quote(self._ctx.harness_session_id, safe="")
        try:
            status, body = await asyncio.wait_for(
                self._request("GET", f"/api/session/{session_id}", None),
                timeout=_GET_TIMEOUT_SECONDS,
            )
            if status != 200:
                return None
            model = _v2_session_model(json.loads(body))
            return model[0] if model is not None else None
        except (TimeoutError, OSError, RuntimeError, aiohttp.ClientError, json.JSONDecodeError):
            return None

    async def compact(self) -> CompactResult:
        if not self._ctx.tui_alive():
            return CompactResult("vetoed", "tui-exited")
        if self._compacting:
            return CompactResult("failed", "compaction already in progress")

        self._compacting = True
        session_id = quote(self._ctx.harness_session_id, safe="")
        session_path = f"/api/session/{session_id}"
        try:
            status, body = await asyncio.wait_for(
                self._request("GET", session_path, None),
                timeout=_GET_TIMEOUT_SECONDS,
            )
            if status != 200:
                return CompactResult("failed", body)
            model = _v2_session_model(json.loads(body))
            if model is None:
                return CompactResult("failed", "session model is unavailable")
            if not self._ctx.tui_alive():
                return CompactResult("vetoed", "tui-exited")

            waiter = asyncio.get_running_loop().create_future()
            self._compact_waiter = waiter
            self._compact_expected_id = None
            status, body = await asyncio.wait_for(
                self._request("POST", f"{session_path}/compact", {}),
                timeout=self._compact_timeout_seconds,
            )
            if status != 200:
                return CompactResult("failed", body)
            admitted = _v2_response_data(json.loads(body))
            inbox_id = admitted.get("id") if admitted is not None else None
            admitted_session_id = admitted.get("sessionID") if admitted is not None else None
            if (
                admitted is None
                or admitted.get("type") != "compaction"
                or not isinstance(inbox_id, str)
                or not inbox_id
                or admitted_session_id != self._ctx.harness_session_id
            ):
                return CompactResult("failed", "invalid compaction response")
            self._compact_expected_id = inbox_id
            self._maybe_finish_compaction()
            succeeded, reason = await asyncio.wait_for(
                waiter,
                timeout=self._compact_timeout_seconds,
            )
            if not succeeded:
                return CompactResult("failed", reason)
        except TimeoutError:
            return CompactResult("failed", "timeout")
        except (OSError, RuntimeError, aiohttp.ClientError, json.JSONDecodeError) as exc:
            return CompactResult("failed", str(exc))
        finally:
            self._compacting = False
            self._compact_expected_id = None
            self._compact_waiter = None
        return CompactResult("ok")


def primary_idle_sensor(ctx: IdleSensorContext) -> IdleSensor | None:
    """Create the sensor hosted by the managed-primary attach launcher."""

    resolved = getattr(ctx.connection, "resolved_version", None)
    version: OpenCodeVersion
    if resolved in ("v1", "v2"):
        version = resolved
    else:
        version = resolve_opencode_version(
            ctx.env.get("MERIDIAN_HARNESS_OPENCODE_VERSION"),
            binary="opencode",
            env=ctx.env,
        )
    if version == "v1":
        logger.warning("OpenCode 1.x idle integration is unsupported; upgrade to OpenCode 2.x")
        return None
    return OpenCodeV2IdleSensor(ctx)


def autocompact_off(env: Mapping[str, str]) -> bool:
    """Return whether OpenCode's own automatic compaction is disabled."""

    return bool(env.get("OPENCODE_DISABLE_AUTOCOMPACT"))


__all__ = [
    "OpenCodeV2IdleSensor",
    "autocompact_off",
    "detect_ttl",
    "primary_idle_sensor",
]
