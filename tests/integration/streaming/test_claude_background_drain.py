"""Regression #655: a turn result is not completion while native tasks run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionConfig,
    HarnessConnection,
    RawHarnessEvent,
)
from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.semantics import NormalizedHarnessEvent
from meridian.lib.state.spawn_signals import spawn_signal_path
from meridian.lib.streaming import resident_drain
from meridian.lib.streaming.drain_plan_factory import build_drain_plan
from meridian.lib.streaming.spawn_drain_loop import SpawnDrainLoop
from meridian.lib.streaming.spawn_session import DrainOutcome, SpawnSession
from meridian.lib.streaming.types import InjectResult
from tests.support.pi import start_row


class _FakeClock:
    """Monotonic clock the coordinator reads; only the test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.reads = 0

    def monotonic(self) -> float:
        self.reads += 1
        return self.now

    def expire_deadline(self) -> None:
        self.now += 3600.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["natural", "done_midturn", "done_poll", "no_result", "stop"],
)
async def test_claude_waits_for_background_snapshot_and_followup_result(
    tmp_path: Path,
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    # The coordinator binds `time.monotonic` at build time; swap only this module's view
    # so the deadline expires when the test says so, not when the host is slow.
    monkeypatch.setattr(resident_drain, "time", clock)
    delivered: list[NormalizedHarnessEvent] = []
    outcomes: list[DrainOutcome] = []
    spawn_id = SpawnId("p-claude-bg")
    messages: list[str] = []
    start_row(tmp_path, str(spawn_id), HarnessId.CLAUDE, None)

    async def inject(
        _id: SpawnId, message: str, source: str = "control_socket", **_: object
    ) -> InjectResult:
        messages.append(message)
        return InjectResult(success=True)

    def frame(kind: str, **payload: object) -> RawHarnessEvent:
        return RawHarnessEvent(event_type=kind, harness_id="claude", payload=payload)

    class Receiver:
        harness_id = HarnessId.CLAUDE
        resident_backend = None
        completion_signals_available = True
        native_task_source = ClaudeConnection.native_task_source
        primary_event_scope = None

        def observe_event_semantics(self, semantics: object) -> None:
            pass

        async def events(self) -> AsyncIterator[RawHarnessEvent]:
            yield frame(
                "system",
                subtype="background_tasks_changed",
                tasks=[{"task_id": "bg", "description": "Write marker", "task_type": "local_bash"}],
            )
            yield frame("result", subtype="success", result="Still running")
            assert not messages
            # Neither the first result nor task completion may publish success:
            # Claude's automatic follow-up turn still owes its own result.
            assert not outcomes
            if scenario == "stop":
                assert plan.coordinator is not None
                close = plan.coordinator.handle_close(intentional_stop=True)
                assert close is not None
                assert close.error == "stream_closed_while_awaiting_done"
                return
            if scenario == "no_result":
                # A stuck active native turn without done is bounded by the deadline.
                yield frame("assistant", message={"content": []})
                clock.expire_deadline()
                await asyncio.sleep(3600)
            if scenario.startswith("done"):
                yield frame("assistant", message={"content": []})
                signal = spawn_signal_path(tmp_path, spawn_id, "done")
                signal.parent.mkdir(parents=True, exist_ok=True)
                signal.touch()
                yield frame("assistant", message={"content": []})
                if scenario == "done_poll":
                    # Several coordinator evaluations between frames, counted not timed.
                    target = clock.reads + 5
                    while clock.reads < target:
                        await asyncio.sleep(0.001)
                    assert not outcomes
                yield frame("result", subtype="success", result="Final done report")
                await asyncio.sleep(3600)  # Native connection stays open after result.
                return
            yield frame("system", subtype="task_notification", task_id="bg", status="completed")
            yield frame("system", subtype="background_tasks_changed", tasks=[])
            assert not outcomes
            yield frame("result", subtype="success", result="Marker read: done")

    receiver = cast("HarnessConnection[Any]", Receiver())
    plan = build_drain_plan(
        runtime_root=tmp_path,
        project_root=tmp_path,
        emit_event=lambda *_: None,
        inject=inject,
        build_spawn_application_service=cast("Any", None),
        spawn_id=spawn_id,
        receiver=receiver,
        config=ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.CLAUDE,
            prompt="probe",
            control_root=tmp_path,
            child_env={},
            resident_poll_seconds=0.01,
            resident_deadline_seconds=0.05,  # fake clock: only bounds real idle sleeps,
        ),
    )
    session = cast("SpawnSession", SimpleNamespace(cancel_sent=False, started_monotonic=0.0))

    def publish(
        _id: SpawnId,
        _session: SpawnSession,
        outcome: DrainOutcome,
        _cleanup: object,
    ) -> DrainOutcome:
        outcomes.append(outcome)
        return outcome

    loop = SpawnDrainLoop(
        sessions={spawn_id: session},
        run_event_hooks=lambda *_: None,
        publish_terminal=publish,
        fan_out_event=lambda _, event: delivered.append(event),
        fan_out_turn_boundary=AsyncMock(),
    )
    try:
        await asyncio.wait_for(
            loop.run(spawn_id=spawn_id, receiver=receiver, drain_plan=plan, tracer=None),
            timeout=60.0,  # hang guard only; no scenario's outcome depends on elapsed time
        )
    except TimeoutError:
        pytest.fail("drain hung")
    if scenario == "stop":
        assert len(outcomes) == 1
        assert outcomes[0].error == "stream_closed_while_awaiting_done"
        return
    if scenario == "no_result":
        assert len(outcomes) == 1
        assert outcomes[0].status == "timed_out"
        reports = [
            event.raw.payload["result"] for event in delivered if event.raw.event_type == "result"
        ]
        assert reports == ["Still running"]
        return
    assert delivered[-1].raw.payload["result"] == (
        "Final done report" if scenario.startswith("done") else "Marker read: done"
    )
    if scenario == "natural":
        assert len(messages) == 1
        assert "meridian spawn done" in messages[0]
        assert "meridian spawn rearm" in messages[0]
    else:
        assert not messages
    assert len(outcomes) == 1
    assert outcomes[0].status == "succeeded"
    assert outcomes[0].authoritative
