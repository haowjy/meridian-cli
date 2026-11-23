"""``agy``'s idle sensor: reports each turn's end to the idle core.

``agy`` fires no event Meridian can subscribe to, so the sensor reads the files the plugin
keeps. The ``PreInvocation`` guard creates ``busy/<conversation>`` and the ``Stop`` hook
removes it (``state.TurnState`` is the one reading of it). When the mark goes, the sensor
reads the transcript past the offset it has already read and reports the turn: a typed
``USER_INPUT`` step means the person came back, while a wake appends only a
``SYSTEM_MESSAGE`` and continues the stretch the last typed turn opened.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meridian.lib.harness.agy.state import TurnState, native_store
from meridian.lib.harness.agy.transcript import AgyTranscriptParser, transcript_path
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.idle_types import IdleEvent

POLL_SECONDS = 1.0


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
        stored_turn_id: Callable[[], str | None],
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._conversation = conversation
        self._turn = TurnState(proc_dir, conversation)
        self._stored_turn_id = stored_turn_id
        self._poll_seconds = poll_seconds
        self._transcript = transcript_path(native_store(), conversation)
        # None until the first read, which takes the whole transcript.
        self._offset: int | None = None
        self._last_typed: str | None = None
        self._user_text: str | None = None

    def on_raw_event(self, event: RawHarnessEvent) -> None:
        _ = event

    async def busy(self) -> bool:
        return self._turn.running()

    async def events(self) -> AsyncIterator[IdleEvent]:
        was_running = self._turn.running()
        while True:
            await asyncio.sleep(self._poll_seconds)
            running = self._turn.running()
            if was_running and not running:
                yield await asyncio.to_thread(self._turn_end)
            was_running = running

    def _turn_end(self) -> IdleEvent:
        first = self._offset is None
        turn = _read_turn(self._read_steps())
        if first:
            # A sensor that restarts mid-session must not take a wake for a return: only a
            # typed step the stored stretch has not seen is one.
            self._last_typed = self._stored_turn_id()
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

    def _read_steps(self) -> list[dict[str, object]]:
        """The complete steps appended since the last read; all of them on the first read or
        after the file shrank."""

        offset = self._offset or 0
        try:
            if self._transcript.stat().st_size < offset:
                offset = 0
            with self._transcript.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except OSError:
            return []
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
