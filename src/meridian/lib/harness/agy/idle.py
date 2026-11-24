"""``agy``'s idle sensor: reports each turn's end to the idle core.

``agy`` fires no event Meridian can subscribe to, so the sensor reads the files the plugin
keeps. The ``PreInvocation`` guard creates ``busy/<conversation>`` and the ``Stop`` hook
removes it (``TurnState`` below reads it, and an Esc). When the mark goes, the sensor
reads the transcript past the offset it has already read and reports the turn: a typed
``USER_INPUT`` step means the person came back, while a wake appends only a
``SYSTEM_MESSAGE`` and continues the stretch the last typed turn opened.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from meridian.lib.harness.agy import inbox
from meridian.lib.harness.agy.state import busy_path, native_store
from meridian.lib.harness.agy.transcript import AgyTranscriptParser, transcript_path
from meridian.lib.harness.agy.watch import watcher_running
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.idle_types import IdleEvent

POLL_SECONDS = 1.0

# The Claude mod's reminder (``claude_runtime/meridian-idle/hooks/keepwarm.ts``
# ``reminderText``) without its closing choices: ``agy`` cannot compact on demand (#1049)
# and has no pings to stop. It ends by restarting the watcher, which the wake already asks
# for, so a model that follows both makes one model call, not two. The mod is TypeScript,
# so the two wordings are kept in step by hand.
STATE_REMINDER = (
    "[Meridian] State reminder. This turn keeps your prompt cache warm while the user is "
    "away; it is about to expire. Run `meridian work show` and update the work item's "
    "recorded state in place: status, decisions with their reasons, open questions, promises "
    "to other sessions, where you stopped. If nothing changed, write nothing. Then start the "
    "watcher again, as the wake notice asks, and end your reply."
)


def _varint(blob: bytes, pos: int) -> tuple[int, int]:
    """The varint at ``pos`` and the position after it; ``IndexError`` when it is cut off."""

    value = shift = 0
    while True:
        byte = blob[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, pos


def _termination_reason(blob: bytes) -> int | None:
    """Field 1 of an ``executor_metadata`` row: ``agy``'s ExecutorTerminationReason."""

    pos = 0
    try:
        while pos < len(blob):
            key, pos = _varint(blob, pos)
            field, wire = key >> 3, key & 7
            if wire == 0:
                value, pos = _varint(blob, pos)
                if field == 1:
                    return value
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            elif wire == 2:
                length, pos = _varint(blob, pos)
                pos += length
            else:
                return None
    except IndexError:
        return None
    return None


_USER_CANCELED = 2


class TurnState:
    """Whether a turn of one conversation is running, for the idle sensor (the watcher waits
    on the ``busy`` file alone: after Esc ``agy`` makes no model call for a finished background
    command until the person types, so reading the cancel there would only make it exit
    uselessly; the idle push still goes out after Esc, which is what this read is for).

    A turn runs while ``busy/<conversation>`` exists, which ``guard.sh`` touches on every
    ``PreInvocation`` and the ``Stop`` hook removes. ``agy`` fires no ``Stop`` on Esc, so
    the mark would stay; but it appends a row to the conversation database's
    ``executor_metadata`` when an execution ends, and field 1 of that row is the
    termination reason (2 is ``USER_CANCELED``, 4 a normal end). Each time the mark is
    touched this reader notes the highest row index; a later row with reason 2 means the
    turn ended. Normal ends are not read: ``Stop`` removes the mark for them, and a
    ``Stop`` continuation's first execution also ends with a row.

    Google documents none of this (the table, the protobuf, the enum): seen on ``agy``
    1.5.15. Every failure to read it, and a missing baseline, leave the turn running, so a
    working session is never taken for an idle one; the baseline is retried on each poll.
    That also means an Esc within about a second of any model invocation (the mark is
    touched on each, and the baseline is noted at the next poll) can be missed, and
    the session then counts as busy until the next turn ends. The one way this fails
    open: a later build that renumbers the enum so that 2 marks an execution ending
    mid-turn would read as an Esc.
    """

    def __init__(self, proc_dir: Path, conversation: str) -> None:
        self._proc_dir = proc_dir
        self._conversation = conversation
        self._touched: int | None = None
        self._baseline: int | None = None
        # ``busy()`` and ``events()`` run on separate worker threads and share one reader.
        self._lock = threading.Lock()

    def running(self) -> bool:
        with self._lock:
            return self._running()

    def _running(self) -> bool:
        try:
            touched = busy_path(self._proc_dir, self._conversation).stat().st_mtime_ns
        except OSError:
            self._touched = None
            return False
        if touched != self._touched:
            self._touched = touched
            self._baseline = self._last_index()
            return True
        if self._baseline is None:
            self._baseline = self._last_index()
            return True
        rows = self._query("SELECT data FROM executor_metadata WHERE idx > ?", self._baseline)
        return rows is None or _USER_CANCELED not in (
            _termination_reason(data) for (data,) in rows if isinstance(data, bytes)
        )

    def _last_index(self) -> int | None:
        rows = self._query("SELECT COALESCE(MAX(idx), -1) FROM executor_metadata")
        return rows[0][0] if rows else None

    def _query(self, sql: str, *params: int) -> list[tuple[Any, ...]] | None:
        """The rows of a read-only query on the conversation's database, None when it fails."""

        database = native_store().parent / "conversations" / f"{self._conversation}.db"
        try:
            with closing(
                sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=0.2)
            ) as db:
                return db.execute(sql, params).fetchall()
        except (sqlite3.Error, ValueError):
            return None


@dataclass(frozen=True)
class _Turn:
    """What the transcript steps read since the last turn end say about it."""

    typed_step: str | None = None
    typed_at_ms: int | None = None
    user_text: str | None = None
    assistant_text: str | None = None


def _created_at_ms(step: dict[str, object]) -> int | None:
    created = step.get("created_at")
    if not isinstance(created, str):
        return None
    try:
        return int(datetime.fromisoformat(created).timestamp() * 1000)
    except ValueError:
        return None


def _read_turn(steps: list[dict[str, object]]) -> _Turn:
    parser = AgyTranscriptParser()
    turn = _Turn()
    for step in steps:
        kind, step_index = step.get("type"), step.get("step_index")
        if kind == "USER_INPUT" and step.get("source") == "USER_EXPLICIT":
            messages = parser.parse(step).messages
            turn = _Turn(
                typed_step=str(step_index),
                typed_at_ms=_created_at_ms(step),
                user_text=messages[0].content if messages else None,
                assistant_text=None,
            )
        elif kind == "PLANNER_RESPONSE":
            messages = parser.parse(step).messages
            if messages and messages[0].tool_call is None:
                turn = _Turn(turn.typed_step, turn.typed_at_ms, turn.user_text, messages[0].content)
    return turn


class AgyIdleSensor:
    """One conversation's turn ends, from its ``busy`` mark and transcript."""

    external_events = True

    def __init__(
        self,
        proc_dir: Path,
        conversation: str,
        *,
        stored_turn_id: str | None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._proc_dir = proc_dir
        self._conversation = conversation
        self._turn = TurnState(proc_dir, conversation)
        self._poll_seconds = poll_seconds
        self._transcript = transcript_path(native_store(), conversation)
        # None until the first read, which takes the whole transcript.
        self._offset: int | None = None
        # A sensor that restarts mid-session must not take a wake for a return: only a typed
        # step the stored stretch has not seen is one.
        self._last_typed = stored_turn_id
        self._user_text: str | None = None

    def on_raw_event(self, event: RawHarnessEvent) -> None:
        _ = event

    async def busy(self) -> bool:
        return await asyncio.to_thread(self._turn.running)

    async def remind(self) -> None:
        """Queue the state reminder as a message from Meridian when the watcher can wake the
        session for it (a running turn shows it at its next model call). With no watcher it
        would wait for the person's next prompt and say they are away, so it is dropped."""

        if await asyncio.to_thread(watcher_running, self._proc_dir, self._conversation):
            await asyncio.to_thread(inbox.enqueue, self._conversation, STATE_REMINDER, None)

    async def events(self) -> AsyncIterator[IdleEvent]:
        was_running = await asyncio.to_thread(self._turn.running)
        while True:
            await asyncio.sleep(self._poll_seconds)
            running = await asyncio.to_thread(self._turn.running)
            if was_running and not running:
                event = await asyncio.to_thread(self._turn_end)
                if event is None:
                    continue  # the transcript was unreadable: report this end at the next poll
                yield event
            was_running = running

    def _turn_end(self) -> IdleEvent | None:
        steps = self._read_steps()
        if steps is None:
            return None
        turn = _read_turn(steps)
        returned = turn.typed_step is not None and turn.typed_step != self._last_typed
        if turn.typed_step is not None:
            self._last_typed = turn.typed_step
            self._user_text = turn.user_text
        return IdleEvent(
            kind="turn_end",
            harness_session_id=self._conversation,
            turn_id=turn.typed_step,
            implies_return=returned,
            returned_at_ms=turn.typed_at_ms if returned else None,
            last_user_text=self._user_text,
            last_assistant_text=turn.assistant_text,
        )

    def _read_steps(self) -> list[dict[str, object]] | None:
        """The complete steps appended since the last read; all of them on the first read or
        after the file shrank; None when it cannot be read, with the offset kept."""

        offset = self._offset or 0
        try:
            if self._transcript.stat().st_size < offset:
                offset = 0
            with self._transcript.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except OSError:
            return None
        complete = data[: data.rfind(b"\n") + 1]
        self._offset = offset + len(complete)
        steps: list[dict[str, object]] = []
        for line in complete.splitlines():
            try:
                step = json.loads(line)
            except ValueError:
                continue
            if isinstance(step, dict):
                steps.append(step)  # pyright: ignore[reportUnknownArgumentType]
        return steps


__all__ = ["AgyIdleSensor"]
