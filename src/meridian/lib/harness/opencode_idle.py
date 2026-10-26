"""OpenCode idle observations: turn ends, user returns, and busy state."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

import aiohttp

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.idle_types import (
    BackgroundTask,
    CacheTtl,
    IdleEvent,
    IdleSensor,
    IdleSensorContext,
)
from meridian.lib.harness.opencode_backend import (
    OpenCodeVersion,
    resolve_opencode_version,
    v2_session_running,
)
from meridian.lib.harness.opencode_running_work import RunningWork
from meridian.lib.utils.json_shapes import json_object

# The primary's turn ends: the execution outcomes of its own session.
TERMINAL_EVENTS = frozenset(
    {
        "session.execution.succeeded",
        "session.execution.failed",
        "session.execution.interrupted",
    }
)

logger = logging.getLogger(__name__)

BackendRequest = Callable[[str, str, Mapping[str, object] | None], Awaitable[tuple[int, str]]]

_GET_TIMEOUT_SECONDS = 10.0
_GET_ERRORS = (TimeoutError, OSError, RuntimeError, ValueError)

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

    async def _get_json(self, path: str) -> object:
        """GET a JSON body from the backend.

        Raises TimeoutError, OSError, RuntimeError (a non-200 status or a client
        error) or ValueError (a body that is not JSON).
        """

        try:
            status, body = await asyncio.wait_for(
                self._request("GET", path, None), timeout=_GET_TIMEOUT_SECONDS
            )
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"GET {path} failed: {exc}") from exc
        if status != 200:
            raise RuntimeError(f"GET {path} returned {status}")
        return json.loads(body)

    async def events(self) -> AsyncIterator[IdleEvent]:
        while True:
            event = await self._events.get()
            if event.kind == "turn_end":
                provider, (user, assistant), background_tasks = await asyncio.gather(
                    self._fetch_provider(),
                    self._fetch_message_facts(),
                    self._fetch_background_tasks(),
                )
                event = replace(
                    event,
                    last_user_text=user,
                    last_assistant_text=assistant,
                    provider=provider,
                    background_tasks=background_tasks,
                )
            yield event

    async def _fetch_provider(self) -> str | None:
        return None

    async def _fetch_background_tasks(self) -> tuple[BackgroundTask, ...]:
        return ()

    async def _fetch_message_facts(self) -> tuple[str | None, str | None]:
        raise NotImplementedError

    async def busy(self) -> bool:
        return self._busy


class OpenCodeIdleSensor(_OpenCodeIdleSensorBase):
    """Translate a managed OpenCode 1.x session's raw stream into idle observations."""

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
        # OpenCode's own compaction (auto or /compact) posts a user message
        # carrying a ``compaction`` part; it is not a user return.
        self._compaction_messages: set[str] = set()

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
        if not self._saw_busy or self._busy:
            return

        self._saw_busy = False
        self._last_anchor_ms = self._now() * 1000
        self._emit("turn_end", turn_id=_event_id(event))

    async def _fetch_message_facts(self) -> tuple[str | None, str | None]:
        session_id = quote(self._ctx.harness_session_id, safe="")
        try:
            return message_excerpts(await self._get_json(f"/session/{session_id}/message?limit=20"))
        except _GET_ERRORS:
            return None, None


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


class OpenCodeV2IdleSensor(_OpenCodeIdleSensorBase):
    """Translate OpenCode 2.x execution and inbox events into idle observations."""

    def __init__(
        self,
        ctx: IdleSensorContext,
        *,
        request: BackendRequest | None = None,
    ) -> None:
        super().__init__(ctx, request=request)
        self._seen_user_inbox_ids: set[str] = set()
        self._pending_user_inbox_ids: set[str] = set()
        self._work = RunningWork(ctx.harness_session_id)
        # Once the sensor has seen the stream, another `server.connected` is a
        # reconnect; the first connection announces itself too.
        self._streaming = False
        # How many times the stream reported the primary's execution starting or ending.
        self._busy_reports = 0
        self._resyncs: set[asyncio.Task[None]] = set()

    def on_raw_event(self, event: RawHarnessEvent) -> None:
        if event.harness_id != self._ctx.harness_id.value:
            return
        reconnected = self._streaming and event.event_type == "server.connected"
        self._streaming = True
        if reconnected:
            resync = asyncio.create_task(self._resync())
            self._resyncs.add(resync)
            resync.add_done_callback(self._resyncs.discard)
        # Shell and child-session events carry another session id, or none.
        effect = self._work.observe(event)
        if effect.answered:
            self._emit("user_prompt")
        if effect.changed:
            self._rearm()
        if _session_id(event) != self._ctx.harness_session_id:
            return

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
            # stretch, so close it again.
            self._emit("user_prompt")

        if event.event_type == "session.execution.started":
            self._report_busy(True)
            return

        if event.event_type not in TERMINAL_EVENTS:
            return

        self._report_busy(False)
        self._emit("turn_end", turn_id=_event_id(event))

    def _report_busy(self, busy: bool) -> None:
        self._busy = busy
        self._busy_reports += 1

    def _rearm(self) -> None:
        """Arm the push again with the work as it stands, after it changed while idle.

        The new arm replaces the schedule of the last turn end; a push already sent
        stays sent. A busy primary ends its turn soon and reports the work then.
        """

        if not self._busy:
            self._emit("turn_end")

    async def _resync(self) -> None:
        """Re-check what the closed stream may have hidden.

        Work may have ended, and the primary's own turn may have ended or started:
        a terminal event that fell in the gap would leave the sensor busy for good,
        and no later turn end comes to correct it.
        """

        reports = self._busy_reports
        changed, running = await asyncio.gather(
            self._work.refresh(self._get_json), self._fetch_running()
        )
        # A report from the stream since then is newer than this snapshot.
        if running is not None and running != self._busy and reports == self._busy_reports:
            self._busy = running
            if not running:
                # The missed turn end, with the work as just refreshed.
                self._emit("turn_end")
                return
        if changed:
            self._rearm()

    async def _fetch_running(self) -> bool | None:
        """Whether the server is running the primary's turn; None when it cannot say."""

        try:
            return v2_session_running(
                await self._get_json("/api/session/active"), self._ctx.harness_session_id
            )
        except _GET_ERRORS as exc:
            logger.warning(
                "OpenCode primary's turn not re-derived after a stream reconnect; "
                "keeping the observed state: %s: %s",
                type(exc).__name__,
                exc,
            )
            return None

    async def events(self) -> AsyncIterator[IdleEvent]:
        try:
            async for event in super().events():
                yield event
        finally:
            # The sidecar cancelling or closing the stream is the sensor's stop.
            for resync in self._resyncs:
                resync.cancel()

    async def _fetch_message_facts(self) -> tuple[str | None, str | None]:
        session_id = quote(self._ctx.harness_session_id, safe="")
        try:
            return _v2_message_excerpts(
                await self._get_json(f"/api/session/{session_id}/message?limit=20&order=desc")
            )
        except _GET_ERRORS:
            return None, None

    async def _fetch_provider(self) -> str | None:
        session_id = quote(self._ctx.harness_session_id, safe="")
        try:
            model = _v2_session_model(await self._get_json(f"/api/session/{session_id}"))
        except _GET_ERRORS:
            return None
        return model[0] if model is not None else None

    async def _fetch_background_tasks(self) -> tuple[BackgroundTask, ...]:
        return self._work.tasks()


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


__all__ = [
    "OpenCodeIdleSensor",
    "OpenCodeV2IdleSensor",
    "detect_ttl",
    "message_excerpts",
    "primary_idle_sensor",
]
