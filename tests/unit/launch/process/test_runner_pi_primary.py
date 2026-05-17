from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.process import runner as runner_module
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


class _FakePiPrimaryConnection:
    def __init__(self, events: list[HarnessEvent], *, stop_result: StopResult) -> None:
        self._events = events
        self._stop_result = stop_result
        self._spawn_id = SpawnId("")
        self._release_input = threading.Event()
        self._agent_end_seen = threading.Event()
        self.sent_messages: list[str] = []

    @property
    def state(self) -> str:
        return "connected"

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.PI

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=True,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def session_id(self) -> str | None:
        return "session-pi-primary"

    @property
    def subprocess_pid(self) -> int | None:
        return 3333

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        self._release_input.set()
        return self._stop_result

    def health(self) -> bool:
        return True

    async def send_user_message(self, text: str) -> None:
        self.sent_messages.append(text)

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        for event in self._events:
            yield event
            if event.event_type == "agent_end":
                self._agent_end_seen.set()
        await asyncio.sleep(60)


def _build_spec() -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )


def test_run_pi_primary_managed_session_does_not_hang_when_input_blocks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    connection = _FakePiPrimaryConnection(
        [
            HarnessEvent(
                event_type="agent_end",
                harness_id="pi",
                payload={"messages": [{"role": "assistant", "stopReason": "error"}]},
            )
        ],
        stop_result=StopResult(escalated=False),
    )

    monkeypatch.setattr(runner_module, "PiRpcConnection", lambda: connection)

    def _blocking_input(_prompt: str) -> str:
        connection._release_input.wait(timeout=2.0)
        return ""

    monkeypatch.setattr("builtins.input", _blocking_input)

    started = time.monotonic()
    exit_code, session_id = runner_module.run_pi_primary_managed_session(
        primary_spawn_id=SpawnId("p-primary-input-hang"),
        control_root=tmp_path,
        task_cwd=None,
        child_env={},
        launch_spec=_build_spec(),
        on_running=None,
    )
    elapsed = time.monotonic() - started

    assert exit_code == 1
    assert session_id == "session-pi-primary"
    assert elapsed < 1.0


def test_run_pi_primary_managed_session_reports_cleanup_escalation_warning(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    connection = _FakePiPrimaryConnection([], stop_result=StopResult(escalated=True))
    monkeypatch.setattr(runner_module, "PiRpcConnection", lambda: connection)
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError()))

    exit_code, session_id = runner_module.run_pi_primary_managed_session(
        primary_spawn_id=SpawnId("p-primary-cleanup-warning"),
        control_root=tmp_path,
        task_cwd=None,
        child_env={},
        launch_spec=_build_spec(),
        on_running=None,
    )

    assert exit_code == 0
    assert session_id == "session-pi-primary"
    captured = capsys.readouterr()
    assert "warning: pi primary cleanup escalated during primary_exit" in captured.err


def test_run_pi_primary_managed_session_streams_text_and_waits_for_explicit_exit(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    connection = _FakePiPrimaryConnection(
        [
            HarnessEvent(
                event_type="message_update",
                harness_id="pi",
                payload={
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "delta": "Hello from Pi.",
                    }
                },
            ),
            HarnessEvent(
                event_type="agent_end",
                harness_id="pi",
                payload={"messages": [{"role": "assistant", "stopReason": "stop"}]},
            ),
        ],
        stop_result=StopResult(escalated=False),
    )
    monkeypatch.setattr(runner_module, "PiRpcConnection", lambda: connection)

    inputs = iter(("say hello", "exit"))

    def _scripted_input(_prompt: str) -> str:
        next_value = next(inputs)
        if next_value == "exit":
            connection._agent_end_seen.wait(timeout=2.0)
        return next_value

    monkeypatch.setattr("builtins.input", _scripted_input)

    exit_code, session_id = runner_module.run_pi_primary_managed_session(
        primary_spawn_id=SpawnId("p-primary-managed-exit"),
        control_root=tmp_path,
        task_cwd=None,
        child_env={},
        launch_spec=_build_spec(),
        on_running=None,
    )

    assert exit_code == 0
    assert session_id == "session-pi-primary"
    assert connection._agent_end_seen.is_set()
    assert connection.sent_messages == ["say hello"]

    captured = capsys.readouterr()
    assert "Hello from Pi." in captured.out
