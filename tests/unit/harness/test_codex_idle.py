from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.codex_bootstrap import bootstrap_turn_prompt
from meridian.lib.harness.codex_idle import CodexIdleSensor, pane_facts, parse_idle_event
from meridian.lib.harness.connections.base import HarnessConnection
from meridian.lib.harness.idle_types import IdleEvent, IdleSensorContext, PinnedIdleSession

FIXTURES = Path(__file__).parents[2] / "fixtures" / "codex_idle"
MAIN_THREAD = "01a11de2-ce9f-7c41-970f-a07df19fa0e4"
PROMPT = "\u203a"


class FakeTmux:
    def __init__(
        self,
        captures: list[str],
        *,
        on_enter: Callable[[], None] | None = None,
    ) -> None:
        self.captures = captures
        self.actions: list[tuple[str, object]] = []
        self.on_enter = on_enter

    async def capture(self, pane: str) -> str | None:
        self.actions.append(("capture", pane))
        if not self.captures:
            return None
        return self.captures.pop(0)

    async def send_literal(self, pane: str, text: str) -> bool:
        self.actions.append(("literal", text))
        return True

    async def send_keys(self, pane: str, *keys: str) -> bool:
        self.actions.append(("keys", keys))
        if keys == ("Enter",) and self.on_enter is not None:
            self.on_enter()
        return True


class FakeClock:
    def __init__(self, *, on_sleep: Callable[[], None] | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.on_sleep = on_sleep

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


def _context(
    *,
    alive: Callable[[], bool] = lambda: True,
    env: Mapping[str, str] | None = None,
) -> IdleSensorContext:
    return IdleSensorContext(
        connection=cast("HarnessConnection[Any]", object()),
        harness_id=HarnessId.CODEX,
        harness_session_id=MAIN_THREAD,
        env=env or {},
        tmux_pane="%42",
        tui_alive=alive,
        spawn_dir=Path("/unused"),
    )


def _rollout_path(tmp_path: Path) -> Path:
    return (
        tmp_path
        / "codex"
        / "sessions"
        / "2026"
        / "10"
        / "10"
        / f"rollout-2026-10-10T00-00-00-{MAIN_THREAD}.jsonl"
    )


def _write_rollout(tmp_path: Path) -> Path:
    path = _rollout_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": MAIN_THREAD}}) + "\n",
        encoding="utf-8",
    )
    return path


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(data)


def _sensor(
    tmp_path: Path,
    tmux: FakeTmux,
    clock: FakeClock,
) -> CodexIdleSensor:
    return CodexIdleSensor(
        _context(env={"CODEX_HOME": str(tmp_path / "codex")}),
        tmux=tmux,
        compact_timeout_seconds=1,
        compact_poll_seconds=0.5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


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

    assert parsed[:2] == [None, None]
    pinned = [event for event in parsed[2:] if event is not None]
    assert [event.harness_session_id for event in pinned] == [MAIN_THREAD] * 2
    assert [event.input_count for event in pinned] == [2, 3]
    assert all(event.implies_return for event in pinned)
    assert pinned[-1].last_user_text == "Run sleep 3 in shell, then reply done."
    assert pinned[-1].last_assistant_text == "done"

    repeated = parse_idle_event(_probe_payloads()[-1], session_reader=read_session)
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
    assert parse_idle_event(bootstrap, session_reader=read_session) is None
    assert last_input_count is None

    real_turn = payload(
        turn="real",
        inputs=[bootstrap_prompt, "actual user prompt"],
        answer="actual answer",
    )
    event = parse_idle_event(real_turn, session_reader=read_session)
    assert event is not None
    assert event.implies_return is True
    assert event.input_count == 2
    assert event.last_user_text == "actual user prompt"

    last_input_count = event.input_count
    repeated = parse_idle_event(real_turn, session_reader=read_session)
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


@pytest.mark.asyncio
async def test_compact_accepts_appended_rollout_compacted_record(tmp_path: Path) -> None:
    rollout = _write_rollout(tmp_path)
    compacted = (FIXTURES / "compacted-0.162.1.jsonl").read_bytes()
    idle = (FIXTURES / "captures" / "idle.txt").read_text(encoding="utf-8")
    tmux = FakeTmux(
        [
            idle,
            idle.replace("Ask Codex to do anything", "/compact"),
        ],
        on_enter=lambda: _append(rollout, compacted),
    )
    clock = FakeClock()
    sensor = _sensor(tmp_path, tmux, clock)

    result = await sensor.compact()

    assert result.result == "ok"
    assert tmux.actions[:4] == [
        ("capture", "%42"),
        ("literal", "/compact"),
        ("capture", "%42"),
        ("keys", ("Enter",)),
    ]
    assert tmux.captures == []


@pytest.mark.asyncio
async def test_compact_times_out_when_rollout_does_not_grow(tmp_path: Path) -> None:
    rollout = _write_rollout(tmp_path)
    _append(rollout, (FIXTURES / "compacted-0.162.1.jsonl").read_bytes())
    idle = (FIXTURES / "captures" / "idle.txt").read_text(encoding="utf-8")
    tmux = FakeTmux([idle, idle.replace("Ask Codex to do anything", "/compact")])
    clock = FakeClock()
    sensor = _sensor(tmp_path, tmux, clock)

    result = await sensor.compact()

    assert (result.result, result.reason) == ("failed", "timeout")


@pytest.mark.asyncio
async def test_compact_ignores_partial_rollout_line_until_complete(
    tmp_path: Path,
) -> None:
    rollout = _write_rollout(tmp_path)
    compacted = (FIXTURES / "compacted-0.162.1.jsonl").read_bytes()
    partial = compacted[: len(compacted) // 2]
    remainder = compacted[len(partial) :]

    def finish_line() -> None:
        if rollout.read_bytes().endswith(partial):
            with rollout.open("ab") as handle:
                handle.write(remainder)

    idle = (FIXTURES / "captures" / "idle.txt").read_text(encoding="utf-8")
    tmux = FakeTmux(
        [idle, idle.replace("Ask Codex to do anything", "/compact")],
        on_enter=lambda: _append(rollout, partial),
    )
    clock = FakeClock(on_sleep=finish_line)
    sensor = _sensor(tmp_path, tmux, clock)

    result = await sensor.compact()

    assert result.result == "ok"
    assert 0.5 in clock.sleeps


@pytest.mark.asyncio
async def test_compact_vetoes_when_rollout_cannot_be_found_without_typing(
    tmp_path: Path,
) -> None:
    idle = (FIXTURES / "captures" / "idle.txt").read_text(encoding="utf-8")
    tmux = FakeTmux([idle])
    clock = FakeClock()
    sensor = _sensor(tmp_path, tmux, clock)

    result = await sensor.compact()

    assert (result.result, result.reason) == ("vetoed", "rollout-not-found")
    assert all(action != "literal" for action, _ in tmux.actions)
    assert all(action != "keys" for action, _ in tmux.actions)


@pytest.mark.asyncio
async def test_compact_vetoes_user_input_and_erases_only_its_typed_text(
    tmp_path: Path,
) -> None:
    _write_rollout(tmp_path)
    tmux = FakeTmux(
        [
            f"{PROMPT} Ask Codex to do anything\n",
            f"{PROMPT} user text/compact\n",
        ]
    )
    clock = FakeClock()
    sensor = _sensor(tmp_path, tmux, clock)

    result = await sensor.compact()

    assert result.result == "vetoed"
    assert tmux.actions[-1] == ("keys", ("BSpace",) * len("/compact"))
    assert ("keys", ("Enter",)) not in tmux.actions
