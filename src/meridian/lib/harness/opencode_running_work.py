"""Background work an OpenCode 2.x primary's own tools left running.

OpenCode resumes a session when a shell started by its ``shell`` tool, or a child
started by its ``subagent`` tool, finishes in the background: the completion
arrives as a synthetic inbox message. It does not resume for a shell made through
``POST /api/shell`` or ``POST /api/session/:id/shell`` (hard-coded ``resume:
false``), nor for a child session made any other way. So only work that a tool
event of the primary named counts; the stream announces everything else too.
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass

from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.idle_types import BackgroundTask
from meridian.lib.utils.json_shapes import json_object

# The bounds the Claude and Pi mods put on the snapshot they hand idle policy.
BACKGROUND_TASK_LIMIT = 16
BACKGROUND_COMMAND_CHAR_CAP = 512

TERMINAL_EVENTS = frozenset(
    {
        "session.execution.succeeded",
        "session.execution.failed",
        "session.execution.interrupted",
    }
)


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


class RunningWork:
    """The primary's background shells and subagents that are still running."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        # Every shell and child of the primary the stream has announced and not yet
        # ended. A tool event naming one of them is what makes it count.
        self._live: dict[str, _Live] = {}
        # The stream reopened while work was named, so its end may have been missed.
        self._reconnected = False

    def observe(self, event: RawHarnessEvent) -> None:
        payload = event.payload
        event_type = event.event_type
        session_id = _text(payload.get("sessionID"))
        if event_type == "server.connected":
            # Every connection announces itself, the first included.
            self._reconnected = bool(self._named())
        elif event_type == "shell.created":
            info = json_object(payload.get("info"))
            shell_id = _text(info.get("id")) if info is not None else None
            command = info.get("command") if info is not None else None
            if shell_id is not None and isinstance(command, str):
                self._live[shell_id] = _Live(command)
        elif event_type in ("shell.exited", "shell.deleted"):
            self._end(_text(payload.get("id")))
        elif event_type == "session.created":
            if session_id is not None and payload.get("parentID") == self._session_id:
                self._live[session_id] = _Live(None)
        elif event_type in TERMINAL_EVENTS:
            self._end(session_id)
        elif session_id != self._session_id:
            return
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

    def _end(self, work_id: str | None) -> None:
        if work_id is not None:
            self._live.pop(work_id, None)

    def _named(self) -> list[_Live]:
        return [live for live in self._live.values() if live.named]

    async def snapshot(
        self, list_running: Callable[[], Awaitable[Collection[str] | None]]
    ) -> tuple[BackgroundTask, ...]:
        """The work to report at a turn end.

        After the stream reopened, an end may have fallen in the gap. Keep only what
        ``list_running`` still reports as running; when it cannot say (``None``),
        report nothing instead of guessing, and check again at the next turn end.
        """

        if self._reconnected and self._named():
            running = await list_running()
            if running is None:
                return ()
            self._live = {key: live for key, live in self._live.items() if key in running}
        self._reconnected = False
        reported = itertools.islice(self._named(), BACKGROUND_TASK_LIMIT)
        return tuple(_background_task(live) for live in reported)
