from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.harness.codex_idle import pane_facts, parse_idle_event
from meridian.lib.harness.idle_types import IdleEvent, PinnedIdleSession

FIXTURES = Path(__file__).parents[2] / "fixtures" / "codex_idle"
MAIN_THREAD = "01a11de2-ce9f-7c41-970f-a07df19fa0e4"


def _probe_payloads() -> list[str]:
    records = [
        json.loads(line)
        for line in (FIXTURES / "notify.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    return [record["argv"][0] for record in records]


def test_parse_idle_event_pins_main_thread_and_tracks_input_growth() -> None:
    counts: dict[str, int | None] = {MAIN_THREAD: None}

    def read_session(session: str) -> PinnedIdleSession | None:
        if session not in counts:
            return None
        return PinnedIdleSession(last_input_count=counts[session])

    parsed: list[IdleEvent | None] = []
    for payload in _probe_payloads():
        event = parse_idle_event(payload, session_reader=read_session)
        parsed.append(event)
        if event is not None:
            counts[event.harness_session_id] = event.input_count

    assert parsed[0] is None
    pinned = [event for event in parsed[1:] if event is not None]
    assert [event.harness_session_id for event in pinned] == [MAIN_THREAD] * 3
    assert [event.input_count for event in pinned] == [1, 2, 3]
    assert all(event.implies_return for event in pinned)

    repeated = parse_idle_event(_probe_payloads()[-1], session_reader=read_session)
    assert repeated is not None
    assert repeated.implies_return is False


@pytest.mark.parametrize(
    ("capture_name", "draft", "busy"),
    [
        ("idle.txt", "no", False),
        ("draft.txt", "yes", False),
        ("busy.txt", "no", True),
        ("busy-final.txt", "no", False),
    ],
)
def test_pane_facts_match_probe_captures(
    capture_name: str,
    draft: str,
    busy: bool,
) -> None:
    capture = (FIXTURES / "captures" / capture_name).read_text(encoding="utf-8")

    facts = pane_facts(capture)
    assert (facts.draft, facts.busy) == (draft, busy)
