"""Background work an OpenCode 2.x primary's own tools left running.

OpenCode resumes a session when a shell started by its ``shell`` tool, or a child
started by its ``subagent`` tool, finishes in the background: the completion
arrives as a synthetic inbox message. It does not resume for a shell made through
``POST /api/shell`` or ``POST /api/session/:id/shell`` (hard-coded ``resume:
false``), nor for a child session made any other way. So only work that a tool
event of the primary named counts; the stream announces everything else too.

A child that waits on a permission prompt does not count while the prompt is open:
it cannot finish until the user answers, so holding the "your turn" push for it
would hide the one thing the user has to do.

This module owns "what work is running": the stream events that tell, and the
server listings that re-check after the stream was closed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import quote

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.idle_types import BackgroundTask
from meridian.lib.utils.json_shapes import json_object

logger = logging.getLogger(__name__)

# The bounds the Claude and Pi mods put on the snapshot they hand idle policy.
BACKGROUND_TASK_LIMIT = 16
BACKGROUND_COMMAND_CHAR_CAP = 512

# GET a path and parse its JSON body. Raises TimeoutError, OSError, RuntimeError
# (a non-200 status or a client error) or ValueError (a body that is not JSON).
GetJson = Callable[[str], Awaitable[object]]

# What `observe` asks the host to do: `changed` re-arms the idle push because the
# reported work just changed; `reopened` means the stream reconnected with work
# named, so `refresh` must re-check it against the server.
Signal = Literal["changed", "reopened"]

# An execution outcome: it ends a child session (the sensor's own turn end is
# `opencode_idle.TERMINAL_EVENTS`, the same three events).
_EXECUTION_ENDED = frozenset(
    {
        "session.execution.succeeded",
        "session.execution.failed",
        "session.execution.interrupted",
    }
)

# Surfaced by the managed primary's permission broker, which consumes the raw
# `permission.asked` and `permission.replied` events: `request/opened` carries the
# ask under `params`, the others carry only the request id.
_ASK_CLOSED = frozenset({"request/resolved", "request/failed", "request/cancelled"})


@dataclass
class _Live:
    # Shell command; None for a child session.
    command: str | None
    # A tool event of the primary named it, so its end wakes the primary.
    named: bool = False


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _tool_work_id(event: RawHarnessEvent, *, running: bool) -> str | None:
    """The shell or child a tool event names in its metadata."""

    metadata = json_object(event.payload.get("metadata"))
    if metadata is None:
        return None
    shell_id = _text(metadata.get("shellID"))
    if shell_id is not None:
        return shell_id
    if running and metadata.get("status") != "running":
        return None
    return _text(metadata.get("sessionID"))


def _synthetic_work_id(event: RawHarnessEvent) -> str | None:
    """The shell or child whose completion a synthetic inbox message reports."""

    item = json_object(event.payload.get("item"))
    if item is None or item.get("type") != "synthetic":
        return None
    payload = json_object(item.get("payload"))
    metadata = json_object(payload.get("metadata")) if payload is not None else None
    if metadata is None:
        return None
    source = metadata.get("source")
    if source == "shell":
        return _text(metadata.get("shellID"))
    if source == "subagent":
        return _text(metadata.get("childID"))
    return None


def _background_task(live: _Live) -> BackgroundTask:
    if live.command is None:
        return BackgroundTask(kind="agent")
    if len(live.command) > BACKGROUND_COMMAND_CHAR_CAP:
        return BackgroundTask(kind="other")
    return BackgroundTask(kind="shell", command=live.command)


def _listed_running_ids(payload: object, *, shells: bool) -> set[str]:
    """Ids a listing reports as still running."""

    response = json_object(payload)
    rows = response.get("data") if response is not None else None
    if not isinstance(rows, list):
        raise ValueError("listing has no data array")
    ids: set[str] = set()
    for raw_row in cast("list[object]", rows):
        row = json_object(raw_row)
        row_id = row.get("id") if row is not None else None
        # The shell listing holds only running shells; a child session is running
        # until it has an outcome.
        if isinstance(row_id, str) and row is not None and (shells or row.get("outcome") is None):
            ids.add(row_id)
    return ids


def _session_directory(payload: object) -> str | None:
    response = json_object(payload)
    session = json_object(response.get("data")) if response is not None else None
    location = json_object(session.get("location")) if session is not None else None
    return _text(location.get("directory")) if location is not None else None


class RunningWork:
    """The primary's background shells and subagents that are still running."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        # Every shell and child of the primary the stream has announced and not yet
        # ended. A tool event naming one of them is what makes it count.
        self._live: dict[str, _Live] = {}
        # Each session under a live child of the primary (the child included) to
        # that child, so a prompt a grandchild raises stops the child counting.
        self._owner: dict[str, str] = {}
        # Open permission prompts: request id to the child that waits on it.
        self._open_asks: dict[str, str] = {}
        self._directory: str | None = None

    def observe(self, event: RawHarnessEvent) -> Signal | None:
        payload = event.payload
        event_type = event.event_type
        session_id = _text(payload.get("sessionID"))
        if event_type == "server.connected":
            # Every connection announces itself, the first included.
            return "reopened" if self._named() else None
        if event_type == "request/opened":
            return self._ask_opened(payload)
        if event_type in _ASK_CLOSED:
            return self._ask_closed(_text(payload.get("request_id")))
        if event_type == "shell.created":
            info = json_object(payload.get("info"))
            shell_id = _text(info.get("id")) if info is not None else None
            command = info.get("command") if info is not None else None
            if shell_id is not None and isinstance(command, str):
                self._live[shell_id] = _Live(command)
        elif event_type in ("shell.exited", "shell.deleted"):
            self._end(_text(payload.get("id")))
        elif event_type == "session.created":
            self._session_created(session_id, _text(payload.get("parentID")))
        elif event_type in _EXECUTION_ENDED:
            self._end(session_id)
        elif session_id != self._session_id:
            return None
        elif event_type == "session.tool.progress":
            work_id = _tool_work_id(event, running=True)
            live = self._live.get(work_id) if work_id is not None else None
            if live is not None:
                live.named = True
        elif event_type == "session.tool.failed":
            # An interrupted foreground tool: the stream reports the turn's end
            # before it reports the aborted shell or child ending.
            self._end(_tool_work_id(event, running=False))
        elif event_type == "session.inbox.enqueued":
            self._end(_synthetic_work_id(event))
        return None

    def _session_created(self, session_id: str | None, parent_id: str | None) -> None:
        if session_id is None:
            return
        if parent_id == self._session_id:
            self._live[session_id] = _Live(None)
            self._owner[session_id] = session_id
        elif parent_id is not None and parent_id in self._owner:
            self._owner[session_id] = self._owner[parent_id]

    def _ask_opened(self, payload: dict[str, object]) -> Signal | None:
        params = json_object(payload.get("params"))
        request_id = _text(payload.get("request_id"))
        asking = _text(params.get("sessionID")) if params is not None else None
        child = self._owner.get(asking) if asking is not None else None
        if request_id is None or child is None:
            return None
        counted = self._counts(child)
        self._open_asks[request_id] = child
        return "changed" if counted != self._counts(child) else None

    def _ask_closed(self, request_id: str | None) -> Signal | None:
        child = self._open_asks.get(request_id) if request_id is not None else None
        if request_id is None or child is None:
            return None
        counted = self._counts(child)
        del self._open_asks[request_id]
        return "changed" if counted != self._counts(child) else None

    def _end(self, work_id: str | None) -> None:
        if work_id is None or self._live.pop(work_id, None) is None:
            return
        self._owner = {key: child for key, child in self._owner.items() if child != work_id}
        self._open_asks = {key: child for key, child in self._open_asks.items() if child != work_id}

    def _counts(self, work_id: str) -> bool:
        live = self._live.get(work_id)
        return live is not None and live.named and work_id not in self._open_asks.values()

    def _named(self) -> list[str]:
        return [work_id for work_id, live in self._live.items() if live.named]

    def tasks(self) -> tuple[BackgroundTask, ...]:
        """The running work to report at a turn end, agents first under the bound."""

        counted = [self._live[work_id] for work_id in self._live if self._counts(work_id)]
        ordered = [live for live in counted if live.command is None] + [
            live for live in counted if live.command is not None
        ]
        return tuple(_background_task(live) for live in ordered[:BACKGROUND_TASK_LIMIT])

    async def refresh(self, get_json: GetJson) -> bool:
        """Drop what ended while the stream was closed; True if the reported work changed.

        Keeps only the ids the server still lists as running. When it cannot say,
        nothing is reported instead of guessing: a hold on work that already ended
        would never lift. Starts that fell in the gap stay unreported, since only a
        tool event can name them.
        """

        before = self.tasks()
        known = set(self._live)
        running = await self._list_running(get_json)
        for work_id in known if running is None else known - running:
            self._end(work_id)
        return self.tasks() != before

    async def _list_running(self, get_json: GetJson) -> set[str] | None:
        session_id = quote(self._session_id, safe="")
        try:
            if self._directory is None:
                self._directory = _session_directory(await get_json(f"/api/session/{session_id}"))
            if self._directory is None:
                raise ValueError("session has no location")
            # The shell listing follows the location, which `location[directory]`
            # selects; a bare `directory` query is ignored.
            shells = await get_json(
                f"/api/shell?location%5Bdirectory%5D={quote(self._directory, safe='')}"
            )
            children = await get_json(f"/api/session?parentID={session_id}&limit=500")
            return _listed_running_ids(shells, shells=True) | _listed_running_ids(
                children, shells=False
            )
        except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "OpenCode running work not re-derived after a stream reconnect; "
                "reporting none: %s: %s",
                type(exc).__name__,
                exc,
            )
            return None
