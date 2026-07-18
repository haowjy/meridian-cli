"""Shared Pi drain scenarios, fakes, and history assertions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    PiSessionRole,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.harness.connections.pi_rpc import pi_subprocess_exit_error
from meridian.lib.harness.semantics import TerminalEventOutcome
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_signals import write_spawn_signal
from meridian.lib.streaming import pi_completion_profile as profile_module
from meridian.lib.streaming import pi_drain as drain_module
from meridian.lib.streaming.control_socket import ControlSocketServer
from meridian.lib.streaming.drain_policy import DrainAction, PiRpcQuiescenceDrainPolicy
from meridian.lib.streaming.pi_drain import PiDrainCoordinator
from meridian.lib.streaming.spawn_manager import SpawnManager
from tests.support.async_determinism import FakeClock, wait_until

if TYPE_CHECKING:
    import pytest

_DEFAULT_SPAWN_ID = SpawnId("p1")


class NoopControlServer(ControlSocketServer):
    def __init__(self) -> None:
        return None

    @property
    def endpoint(self) -> str:
        return "noop://control"

    @property
    def discovery_path(self) -> Path:
        return Path("noop-control")

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class FakePiConnection(HarnessConnection[ResolvedLaunchSpec]):
    def __init__(self, events: list[HarnessEvent]) -> None:
        self._events = events
        self._spawn_id = SpawnId("")
        self._state: ConnectionState = "created"
        self.stop_reasons: list[str | None] = []

    @property
    def state(self) -> ConnectionState:
        return self._state

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
        return "ses-pi"

    @property
    def subprocess_pid(self) -> int | None:
        return 4242

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        del spec
        self._spawn_id = config.spawn_id
        self._state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        del progress
        self.stop_reasons.append(reason)
        self._state = "stopped"
        return StopResult()

    def health(self) -> bool:
        return self._state == "connected"

    async def send_user_message(self, text: str) -> None:
        del text

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        for event in self._events:
            yield event


def pi_event(event_type: str, payload: dict[str, object] | None = None) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, harness_id="pi", payload=payload or {})


def pi_process_exit_event(return_code: int) -> HarnessEvent:
    """Model the connection-close event emitted when the Pi process exits non-zero."""
    return pi_event(
        "error/connectionClosed",
        {
            "type": "error/connectionClosed",
            "message": pi_subprocess_exit_error(return_code),
        },
    )


def write_pi_bash_record(
    runtime_root: Path, spawn_id: SpawnId, *, running: bool = True
) -> None:
    """Write the managed-bash disk evidence used by the Pi extension."""
    path = runtime_root / "pi-bash" / str(spawn_id) / "bash-records.json"
    write_json(
        path,
        {
            "records": {
                "b1": {
                    "bash_id": "b1",
                    "is_tracked": True,
                    "is_background": True,
                    "status": "running" if running else "exited",
                }
            }
        },
    )


@dataclass
class PiDrainScenario:
    """One scripted Pi coordinator with lifecycle, disk, and clock controls."""

    runtime_root: Path
    spawn_id: SpawnId
    coordinator: PiDrainCoordinator
    connection: FakePiConnection
    clock: FakeClock
    phases: list[dict[str, object]]
    nudges: list[str]
    cleanups: list[str]
    phase_errors_enabled: asyncio.Event

    @classmethod
    async def start(
        cls,
        runtime_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        spawn_id: SpawnId = _DEFAULT_SPAWN_ID,
        child_wave_timeout_seconds: float | None = None,
        nudge_idle_seconds: float = 5.0,
        nudge_interval_seconds: float = 5.0,
        nudge_raises: bool = False,
        sent_messages: list[str] | None = None,
        cleanup_configured: bool = True,
        start_micro_drain: bool = False,
        mark_idle: bool = False,
        cancel_descendants: Any = None,
        patch_clock: bool = True,
    ) -> PiDrainScenario:
        clock = FakeClock(start=100.0)
        if patch_clock:
            monkeypatch.setattr(drain_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(profile_module, "PI_DONE_NUDGE_IDLE_DELAY_SECONDS", nudge_idle_seconds)
        monkeypatch.setattr(
            profile_module, "COMPLETION_NUDGE_INTERVAL_SECONDS", nudge_interval_seconds
        )
        phases: list[dict[str, object]] = []
        nudges = sent_messages if sent_messages is not None else []
        cleanups: list[str] = []
        phase_errors_enabled = asyncio.Event()
        connection = FakePiConnection([])
        await connection.start(_config(runtime_root, spawn_id), _spec())

        def emit_phase(*, phase: str, session_role: str | None, **payload: object) -> None:
            assert session_role == "spawned"
            phases.append({"phase": phase, **payload})
            if phase_errors_enabled.is_set():
                raise RuntimeError(f"phase emission failed: {phase}")

        async def nudge(message: str) -> None:
            nudges.append(message)
            if nudge_raises:
                raise RuntimeError("advisory nudge failed")

        async def cleanup(reason: str) -> None:
            cleanups.append(reason)

        cleanup_callback = cancel_descendants
        if cleanup_callback is None and cleanup_configured:
            cleanup_callback = cleanup
        coordinator = PiDrainCoordinator.for_connection(
            runtime_root=runtime_root,
            spawn_id=spawn_id,
            receiver=connection,
            session_role="spawned",
            child_wave_timeout_seconds=child_wave_timeout_seconds,
            emit_phase=emit_phase,
            cancel_descendants=cleanup_callback,
            send_done_nudge=nudge,
        )
        await coordinator.start()
        coordinator.set_policy(
            PiRpcQuiescenceDrainPolicy(quiescence_check=coordinator.is_quiescent)
        )
        scenario = cls(
            runtime_root,
            spawn_id,
            coordinator,
            connection,
            clock,
            phases,
            nudges,
            cleanups,
            phase_errors_enabled,
        )
        if mark_idle or start_micro_drain:
            await scenario.idle()
        if start_micro_drain:
            await scenario.terminal()
        return scenario

    async def stop(self) -> None:
        await self.coordinator.stop()

    async def observe(
        self,
        event_type: str,
        payload: dict[str, object] | None = None,
        transition: str | None = None,
    ) -> None:
        await self.coordinator.observe_event(pi_event(event_type, payload), transition)

    async def idle(self) -> None:
        await self.observe("agent_end", transition="idle")

    async def terminal(self):  # type: ignore[no-untyped-def]
        event = pi_event("agent_end")
        return await self.coordinator.handle_terminal_event(
            event,
            TerminalEventOutcome(status="succeeded", exit_code=0),
            DrainAction(terminate=True, emit_turn_boundary=False),
        )

    async def timeout(self, seconds: float = 0.0):  # type: ignore[no-untyped-def]
        self.clock.advance(seconds)
        return await self.coordinator.handle_timeout()

    def row(
        self,
        spawn_id: str,
        *,
        parent_id: str | None,
        status: SpawnStatus = "running",
        harness: HarnessId = HarnessId.CODEX,
    ) -> None:
        start_row(self.runtime_root, spawn_id, harness, parent_id, status=status)

    def running_bash(self, *, running: bool = True) -> None:
        write_pi_bash_record(self.runtime_root, self.spawn_id, running=running)

    def done(self) -> None:
        write_spawn_signal(self.runtime_root, self.spawn_id, "done")


async def start_pi_manager(
    runtime_root: Path,
    connection: FakePiConnection,
    *,
    spawn_id: SpawnId,
    session_role: PiSessionRole = "spawned",
    child_wave_timeout_seconds: float | None = None,
) -> SpawnManager:
    """Start a SpawnManager around one scripted Pi connection."""

    async def start_connection(
        config: ConnectionConfig, spec: ResolvedLaunchSpec
    ) -> HarnessConnection[ResolvedLaunchSpec]:
        await connection.start(config, spec)
        return connection

    manager = SpawnManager(
        runtime_root=runtime_root,
        project_root=runtime_root,
        start_connection=start_connection,
        control_server_factory=lambda _spawn_id, _socket_path, _manager: NoopControlServer(),
    )
    await manager.start_spawn(
        ConnectionConfig(
            spawn_id=spawn_id,
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=runtime_root,
            child_env={},
            pi_session_role=session_role,
            pi_child_wave_timeout_seconds=child_wave_timeout_seconds,
        ),
        _spec(),
    )
    return manager


def read_history(runtime_root: Path, spawn_id: SpawnId) -> list[dict[str, Any]]:
    path = runtime_root / "spawns" / str(spawn_id) / "history.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def read_history_phases(runtime_root: Path, spawn_id: SpawnId) -> list[str]:
    return [
        cast("str", event.get("payload", {}).get("phase"))
        for event in read_history(runtime_root, spawn_id)
        if event.get("event_type") == "meridian.pi.lifecycle.phase"
    ]


async def wait_for_history_phase(
    runtime_root: Path, spawn_id: SpawnId, phase: str, *, count: int = 1
) -> list[str]:
    await wait_until(
        lambda: read_history_phases(runtime_root, spawn_id).count(phase) >= count,
        timeout=5.0,
        description=f"{phase} lifecycle phase",
    )
    return read_history_phases(runtime_root, spawn_id)


def history_has_phase(runtime_root: Path, spawn_id: SpawnId, phase: str) -> bool:
    path = runtime_root / "spawns" / str(spawn_id) / "history.jsonl"
    return path.exists() and phase in read_history_phases(runtime_root, spawn_id)


def history_has_event(runtime_root: Path, spawn_id: SpawnId, event_type: str) -> bool:
    path = runtime_root / "spawns" / str(spawn_id) / "history.jsonl"
    return path.exists() and any(
        event.get("event_type") == event_type for event in read_history(runtime_root, spawn_id)
    )


def read_phase_events(runtime_root: Path, spawn_id: SpawnId, phase: str) -> list[dict[str, Any]]:
    return [
        event
        for event in read_history(runtime_root, spawn_id)
        if event.get("event_type") == "meridian.pi.lifecycle.phase"
        and event.get("payload", {}).get("phase") == phase
    ]


def start_row(
    runtime_root: Path,
    spawn_id: str,
    harness: HarnessId,
    parent_id: str | None,
    *,
    status: SpawnStatus = "running",
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=SpawnId(spawn_id),
        chat_id=spawn_id,
        parent_id=parent_id,
        model="test-model",
        agent="test-agent",
        harness=harness.value,
        prompt="hello",
        status=status,
    )


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _config(runtime_root: Path, spawn_id: SpawnId) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=HarnessId.PI,
        prompt="hello",
        control_root=runtime_root,
        child_env={},
        pi_session_role="spawned",
    )


def _spec() -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
