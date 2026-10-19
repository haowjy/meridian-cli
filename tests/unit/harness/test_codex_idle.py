from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.codex_bootstrap import bootstrap_turn_prompt
from meridian.lib.harness.codex_idle import CodexIdleSensor, pane_busy, parse_idle_event
from meridian.lib.harness.connections.base import HarnessConnection
from meridian.lib.harness.idle_types import IdleEvent, IdleSensorContext, PinnedIdleSession

FIXTURES = Path(__file__).parents[2] / "fixtures" / "codex_idle"
MAIN_THREAD = "01a11de2-ce9f-7c41-970f-a07df19fa0e4"


def _context(
    *,
    alive: Callable[[], bool] = lambda: True,
) -> IdleSensorContext:
    return IdleSensorContext(
        connection=cast("HarnessConnection[Any]", object()),
        harness_id=HarnessId.CODEX,
        harness_session_id=MAIN_THREAD,
        env={},
        tmux_pane="%42",
        tui_alive=alive,
        spawn_dir=Path("/unused"),
    )


def _probe_payloads() -> list[str]:
    records = [
        json.loads(line)
        for line in (FIXTURES / "notify.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    return [record["argv"][0] for record in records]


def test_parse_idle_event_pins_main_thread_and_tracks_input_growth(tmp_path: Path) -> None:
    counts: dict[str, int | None] = {MAIN_THREAD: None}
    env = {"CODEX_HOME": str(tmp_path / "codex")}

    def read_session(session: str) -> PinnedIdleSession | None:
        if session not in counts:
            return None
        return PinnedIdleSession(last_input_count=counts[session])

    parsed: list[IdleEvent | None] = []
    for payload in _probe_payloads():
        event = parse_idle_event(payload, session_reader=read_session, env=env)
        parsed.append(event)
        if event is not None:
            counts[event.harness_session_id] = event.input_count

    assert parsed[:2] == [None, None]
    pinned = [event for event in parsed[2:] if event is not None]
    assert [event.harness_session_id for event in pinned] == [MAIN_THREAD] * 2
    assert [event.input_count for event in pinned] == [2, 3]
    assert all(event.implies_return for event in pinned)
    assert pinned[-1].last_user_text == "Run sleep 3 in shell, then reply done."
    assert pinned[-1].last_assistant_text == "done"

    repeated = parse_idle_event(_probe_payloads()[-1], session_reader=read_session, env=env)
    assert repeated is not None
    assert repeated.implies_return is False


def test_parse_idle_event_skips_bootstrap_but_tracks_the_next_real_turn() -> None:
    last_input_count: int | None = None
    bootstrap_prompt = bootstrap_turn_prompt("coder")

    def read_session(session: str) -> PinnedIdleSession | None:
        if session != MAIN_THREAD:
            return None
        return PinnedIdleSession(last_input_count=last_input_count)

    def payload(*, turn: str, inputs: list[str], answer: str) -> str:
        return json.dumps(
            {
                "type": "agent-turn-complete",
                "thread-id": MAIN_THREAD,
                "turn-id": turn,
                "input-messages": inputs,
                "last-assistant-message": answer,
            }
        )

    bootstrap = payload(
        turn="bootstrap",
        inputs=[bootstrap_prompt],
        answer=bootstrap_prompt,
    )
    assert parse_idle_event(bootstrap, session_reader=read_session, env={}) is None
    assert last_input_count is None

    real_turn = payload(
        turn="real",
        inputs=[bootstrap_prompt, "actual user prompt"],
        answer="actual answer",
    )
    event = parse_idle_event(real_turn, session_reader=read_session, env={})
    assert event is not None
    assert event.implies_return is True
    assert event.input_count == 2
    assert event.last_user_text == "actual user prompt"

    last_input_count = event.input_count
    repeated = parse_idle_event(real_turn, session_reader=read_session, env={})
    assert repeated is not None
    assert repeated.implies_return is False


@pytest.mark.parametrize(
    ("capture_name", "busy"),
    [
        ("idle.txt", False),
        ("busy.txt", True),
        ("busy-final.txt", False),
    ],
)
def test_pane_busy_matches_probe_captures(
    capture_name: str,
    busy: bool,
) -> None:
    capture = (FIXTURES / "captures" / capture_name).read_text(encoding="utf-8")

    assert pane_busy(capture) is busy


class FakeTmux:
    def __init__(self, capture: str | None) -> None:
        self.capture_text = capture
        self.panes: list[str] = []

    async def capture(self, pane: str) -> str | None:
        self.panes.append(pane)
        return self.capture_text


@pytest.mark.asyncio
async def test_busy_reads_the_pane_only_while_the_tui_lives() -> None:
    busy = (FIXTURES / "captures" / "busy.txt").read_text(encoding="utf-8")
    tmux = FakeTmux(busy)
    assert await CodexIdleSensor(_context(), tmux=tmux).busy() is True
    assert tmux.panes == ["%42"]

    dead = FakeTmux(busy)
    assert await CodexIdleSensor(_context(alive=lambda: False), tmux=dead).busy() is False
    assert dead.panes == []
