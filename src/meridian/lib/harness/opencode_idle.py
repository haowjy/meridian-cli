"""OpenCode idle observations and compaction actuation."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

import aiohttp

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.connections.opencode_v2_http import v2_session_idle_marker
from meridian.lib.harness.idle_types import (
    CacheTtl,
    CompactCosts,
    CompactResult,
    IdleEvent,
    IdleFacts,
    IdleSensor,
    IdleSensorContext,
)
from meridian.lib.harness.opencode_backend import OpenCodeVersion, resolve_opencode_version
from meridian.lib.utils.json_shapes import json_object

BackendRequest = Callable[[str, str, Mapping[str, object] | None], Awaitable[tuple[int, str]]]

_GET_TIMEOUT_SECONDS = 10.0
_SUMMARIZE_TIMEOUT_SECONDS = 300.0

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


def compact_costs(
    *, ttl_seconds: int | None
) -> CompactCosts | None:
    """Return measured remote-compaction economics for OpenCode's OpenAI route."""

    _ = ttl_seconds
    # One OpenCode 2.x OpenAI compaction read 187,520 cached tokens at 0.1x
    # with 730 uncached tokens; the V2 request retains a 15k-token text tail.
    return CompactCosts(
        w=1.0,
        c=0.104,
        u=1_000,
        o=8.0,
        summary_tokens=1_600,
        residue_tokens=16_500,
    )


def _properties(event: RawHarnessEvent) -> Mapping[str, object]:
    return json_object(event.payload.get("properties")) or event.payload


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
    info = json_object(_properties(event).get("info"))
    if info is None or info.get("role") != "user":
        return None
    message_id = info.get("id")
    timestamp = json_object(info.get("time"))
    created_ms = _epoch_ms(timestamp.get("created")) if timestamp is not None else None
    if not isinstance(message_id, str) or not message_id or created_ms is None:
        return None
    return message_id, created_ms


def _message_part(event: RawHarnessEvent) -> tuple[str, str] | None:
    if event.event_type != "message.part.updated":
        return None
    part = json_object(_properties(event).get("part"))
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
    status = json_object(_properties(event).get("status"))
    value = status.get("type") if status is not None else None
    return value if isinstance(value, str) else None


def _text_content(value: object) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    if not isinstance(value, list):
        return None
    text: list[str] = []
    for raw_part in cast("list[object]", value):
        part = json_object(raw_part)
        if part is None or part.get("type") != "text":
            continue
        if part.get("synthetic") or part.get("ignored"):
            continue
        part_text = part.get("text")
        if isinstance(part_text, str) and part_text.strip():
            text.append(part_text)
    return " ".join(text) or None


def message_excerpts(payload: object) -> tuple[str | None, str | None]:
    """Extract the last user and assistant text parts from a messages response."""

    if not isinstance(payload, list):
        return None, None
    user: str | None = None
    assistant: str | None = None
    for raw_message in cast("list[object]", payload):
        message = json_object(raw_message)
        if message is None:
            continue
        info = json_object(message.get("info"))
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


class _OpenCodeIdleSensorBase:
    """Shared OpenCode sensor plumbing independent of backend dialect."""

    def __init__(
        self,
        ctx: IdleSensorContext,
        *,
        request: BackendRequest | None = None,
    ) -> None:
        self._ctx = ctx
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
                provider, (user, assistant, context_tokens) = await asyncio.gather(
                    self._fetch_provider(),
                    self._fetch_message_facts(),
                )
                event = replace(
                    event,
                    last_user_text=user,
                    last_assistant_text=assistant,
                    provider=provider,
                    context_tokens=context_tokens,
                )
            yield event

    async def _fetch_provider(self) -> str | None:
        return None

    async def _fetch_message_facts(
        self,
    ) -> tuple[str | None, str | None, int | None]:
        raise NotImplementedError

    async def _fetch_context_tokens(self) -> int | None:
        return None

    async def facts(self) -> IdleFacts:
        return IdleFacts(
            busy=self._busy,
            agents_running=0,
            context_tokens=await self._fetch_context_tokens(),
            harness_autocompact_off=False,
        )


class OpenCodeIdleSensor(_OpenCodeIdleSensorBase):
    """Translate a managed OpenCode 1.x session's raw stream into idle facts."""

    def __init__(
        self,
        ctx: IdleSensorContext,
        *,
        now: Callable[[], float] = time.time,
        request: BackendRequest | None = None,
    ) -> None:
        super().__init__(ctx, request=request)
        self._now = now
        self._saw_busy = False
        self._last_anchor_ms = now() * 1000
        self._seen_user_messages: set[str] = set()
        self._pending_user_messages: set[str] = set()
        self._compaction_messages: set[str] = set()
        self._compacting = False

    def _classify_pending_user_messages(self) -> None:
        for message_id in tuple(self._pending_user_messages):
            self._pending_user_messages.discard(message_id)
            self._seen_user_messages.add(message_id)
            if message_id not in self._compaction_messages:
                self._emit("user_prompt")
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
            if self._compacting:
                self._compaction_messages.add(message_id)
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
            return
        if status == "idle":
            self._busy = False
            return
        if event.event_type != "session.idle":
            return
        self._compacting = False
        if not self._saw_busy or self._busy:
            return

        self._saw_busy = False
        self._last_anchor_ms = self._now() * 1000
        self._emit("turn_end", turn_id=_event_id(event))

    async def _fetch_message_facts(
        self,
    ) -> tuple[str | None, str | None, int | None]:
        session_id = quote(self._ctx.harness_session_id, safe="")
        try:
            status, body = await asyncio.wait_for(
                self._request("GET", f"/session/{session_id}/message?limit=20", None),
                timeout=_GET_TIMEOUT_SECONDS,
            )
            if status != 200:
                return None, None, None
            user, assistant = message_excerpts(json.loads(body))
            return user, assistant, None
        except (TimeoutError, OSError, RuntimeError, aiohttp.ClientError, json.JSONDecodeError):
            return None, None, None

    async def compact(self) -> CompactResult:
        if not self._ctx.tui_alive():
            return CompactResult("vetoed", "tui-exited")
        session_id = quote(self._ctx.harness_session_id, safe="")
        session_path = f"/session/{session_id}"
        try:
            status, body = await asyncio.wait_for(
                self._request("GET", session_path, None),
                timeout=_GET_TIMEOUT_SECONDS,
            )
            if status != 200:
                return CompactResult("failed", body)
            session = json_object(json.loads(body))
            model = json_object(session.get("model")) if session is not None else None
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
            self._compacting = True
            completed = False
            try:
                status, body = await asyncio.wait_for(
                    self._request(
                        "POST",
                        f"{session_path}/summarize",
                        {"providerID": provider, "modelID": model_id},
                    ),
                    timeout=_SUMMARIZE_TIMEOUT_SECONDS,
                )
                completed = status == 200 and body.strip() == "true"
            finally:
                if not completed:
                    self._compacting = False
        except TimeoutError:
            return CompactResult("failed", "timeout")
        except (OSError, RuntimeError, aiohttp.ClientError, json.JSONDecodeError) as exc:
            return CompactResult("failed", str(exc))
        if completed:
            return CompactResult("ok")
        return CompactResult("failed", body)


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
    item = json_object(event.payload.get("item"))
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
    response = json_object(payload)
    if response is None:
        return None
    return json_object(response.get("data"))


def _v2_session_model(payload: object) -> tuple[str, str] | None:
    session = _v2_response_data(payload)
    model = json_object(session.get("model")) if session is not None else None
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
    response = json_object(payload)
    messages = response.get("data") if response is not None else None
    if not isinstance(messages, list):
        return None, None
    # The V2 request explicitly asks for newest-first results. The shared
    # extractor retains the last text for each role, so restore timeline order.
    return message_excerpts(list(reversed(cast("list[object]", messages))))


def v2_message_context_tokens(payload: object) -> int | None:
    """Return the newest trustworthy assistant main-context usage."""

    response = json_object(payload)
    messages = response.get("data") if response is not None else None
    if not isinstance(messages, list):
        return None
    for raw_message in cast("list[object]", messages):
        message = json_object(raw_message)
        if message is None:
            continue
        info = json_object(message.get("info"))
        facts = info if info is not None else message
        role = facts.get("role") or message.get("role") or message.get("type")
        if role != "assistant":
            continue
        if facts.get("mode") == "compaction" or facts.get("summary") is True:
            return None
        tokens = json_object(facts.get("tokens"))
        if tokens is None:
            continue
        cache = json_object(tokens.get("cache"))
        values = (
            tokens.get("input"),
            cache.get("read") if cache is not None else None,
            cache.get("write") if cache is not None else None,
        )
        total = sum(
            value
            for value in values
            if not isinstance(value, bool) and isinstance(value, int) and value > 0
        )
        if total <= 0:
            continue
        return total
    return None


def _v2_has_completed_compaction_message(payload: object, expected_id: str) -> bool:
    response = json_object(payload)
    messages = response.get("data") if response is not None else None
    if not isinstance(messages, list):
        return False
    for raw_message in cast("list[object]", messages):
        message = json_object(raw_message)
        if message is None:
            continue
        info = json_object(message.get("info"))
        identity = next(
            (
                candidate
                for candidate in (message, info)
                if candidate is not None
                and candidate.get("id") == expected_id
                and candidate.get("type") == "compaction"
            ),
            None,
        )
        if identity is None:
            continue
        raw_status = message.get("status")
        if not isinstance(raw_status, str) and info is not None:
            raw_status = info.get("status")
        return isinstance(raw_status, str) and raw_status.strip().lower() == "completed"
    return False


def _v2_session_idle_after(payload: object, admitted_at_ms: float | None) -> bool:
    if admitted_at_ms is None:
        return False
    outcome, idle_at_ms = v2_session_idle_marker(payload)
    if isinstance(outcome, str) and outcome.strip().lower() not in {"", "succeeded"}:
        return False
    return idle_at_ms is not None and idle_at_ms >= admitted_at_ms


class OpenCodeV2IdleSensor(_OpenCodeIdleSensorBase):
    """Translate OpenCode 2.x execution and inbox events into idle facts."""

    def __init__(
        self,
        ctx: IdleSensorContext,
        *,
        request: BackendRequest | None = None,
        compact_timeout_seconds: float = _SUMMARIZE_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(ctx, request=request)
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

    async def _fetch_message_facts(
        self,
    ) -> tuple[str | None, str | None, int | None]:
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
                return None, None, None
            payload = json.loads(body)
            user, assistant = _v2_message_excerpts(payload)
            return user, assistant, v2_message_context_tokens(payload)
        except (TimeoutError, OSError, RuntimeError, aiohttp.ClientError, json.JSONDecodeError):
            return None, None, None

    async def _fetch_context_tokens(self) -> int | None:
        _, _, context_tokens = await self._fetch_message_facts()
        return context_tokens

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

    async def _confirm_durable_compaction(
        self,
        *,
        inbox_id: str,
        admitted_at_ms: float | None,
    ) -> bool:
        """Confirm a completed compaction after a live-only stream gap."""

        session_id = quote(self._ctx.harness_session_id, safe="")
        session_path = f"/api/session/{session_id}"
        try:
            messages_status, messages_body = await asyncio.wait_for(
                self._request(
                    "GET",
                    f"{session_path}/message?limit=20&order=desc&type=compaction",
                    None,
                ),
                timeout=_GET_TIMEOUT_SECONDS,
            )
            if messages_status != 200 or not _v2_has_completed_compaction_message(
                json.loads(messages_body), inbox_id
            ):
                return False
            session_status, session_body = await asyncio.wait_for(
                self._request("GET", session_path, None),
                timeout=_GET_TIMEOUT_SECONDS,
            )
            return session_status == 200 and _v2_session_idle_after(
                json.loads(session_body), admitted_at_ms
            )
        except (TimeoutError, OSError, RuntimeError, aiohttp.ClientError, json.JSONDecodeError):
            return False

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
            admitted_time = json_object(admitted.get("time"))
            admitted_at_ms = (
                _epoch_ms(admitted_time.get("created")) if admitted_time is not None else None
            )
            try:
                succeeded, reason = await asyncio.wait_for(
                    waiter,
                    timeout=self._compact_timeout_seconds,
                )
            except TimeoutError:
                if await self._confirm_durable_compaction(
                    inbox_id=inbox_id,
                    admitted_at_ms=admitted_at_ms,
                ):
                    return CompactResult("ok")
                return CompactResult("failed", "timeout")
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


def primary_idle_sensor(ctx: IdleSensorContext) -> IdleSensor:
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
        return OpenCodeIdleSensor(ctx)
    return OpenCodeV2IdleSensor(ctx)


def autocompact_off(env: Mapping[str, str]) -> bool:
    """Return whether OpenCode's own automatic compaction is disabled."""

    return bool(env.get("OPENCODE_DISABLE_AUTOCOMPACT"))


__all__ = [
    "OpenCodeIdleSensor",
    "OpenCodeV2IdleSensor",
    "autocompact_off",
    "compact_costs",
    "detect_ttl",
    "message_excerpts",
    "primary_idle_sensor",
    "v2_message_context_tokens",
]
