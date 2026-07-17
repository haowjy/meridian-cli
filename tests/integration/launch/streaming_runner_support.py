# qa-validated: test-suite-redesign
# qa-validated: pi-rpc-quiescence
"""Shared fakes for streaming runner lifecycle tests."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.harness.launch_spec import ResolvedLaunchSpec
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.harness.semantics import (
    PrimaryEventScope,
    codex_primary_event_scope,
    opencode_primary_event_scope,
)
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchRuntime,
    SpawnRequest,
)
from meridian.lib.platform.process_scope import ProcessScopeSnapshot
from meridian.lib.platform.process_scope import fallback as process_scope_fallback
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.state.spawn_signals import write_spawn_signal
from tests.support.fakes import FakeClock
from tests.support.pi_extensions import configure_pi_extension_projection

streaming_runner_module = importlib.import_module("meridian.lib.launch.streaming_runner")


@pytest.fixture(autouse=True)
def _pi_extension_projection_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_pi_extension_projection(monkeypatch, tmp_path)


class _FakeControlSocketServer:
    def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: object) -> None:
        _ = spawn_id, manager
        self.socket_path = socket_path

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        return None


class _IdleResidentBackend:
    def health_status(self) -> object:
        return "continue"

    def set_awaiting_done(self, awaiting: bool) -> None:
        _ = awaiting

    async def begin_followup_turn(self, message: str) -> None:
        _ = message


class _ReportThenHangConnection:
    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._project_root: Path | None = None
        self._session_id = "thread-watchdog"
        self._resident_backend = _IdleResidentBackend()
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="interrupt_restart",
            supports_steer=True,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def subprocess_pid(self) -> int | None:
        return 4242

    @property
    def primary_event_scope(self) -> None:
        return None

    @property
    def resident_backend(self) -> object:
        return self._resident_backend

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        self._project_root = config.control_root
        self.state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        self.state = "stopped"
        return StopResult()

    def health(self) -> bool:
        return self.state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        project_root = self._project_root
        assert project_root is not None
        spawn_dir = resolve_spawn_log_dir(project_root, self._spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        (spawn_dir / "report.md").write_text(
            "# Done\n\nWatchdog fallback completed.\n",
            encoding="utf-8",
        )
        yield HarnessEvent(
            event_type="item/completed",
            harness_id="codex",
            payload={
                "item": {"id": "msg-1", "type": "agentMessage", "text": "done"},
                "threadId": self._session_id,
                "turnId": "turn-1",
            },
        )
        while True:
            await asyncio.sleep(3600)


class _OpenCodeTerminalWithScopeConnection:
    harness = HarnessId.OPENCODE
    terminal_event_type = "session.idle"
    terminal_payload_session_key = "sessionID"

    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._project_root: Path | None = None
        self._session_id = "session-opencode-cleanup"
        self._resident_backend = _IdleResidentBackend()
        self._scope_snapshot: ProcessScopeSnapshot | None = ProcessScopeSnapshot(
            scope_id="backend",
            owner_policy="spawn_owned",
            owner_id="",
            role="harness_backend",
            containment="pid_tree_fallback",
            root_pid=73737,
            root_created_at_epoch=12_345.0,
            pgid=None,
            job_name=None,
            degraded_reason=None,
        )
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="http_post",
            supports_steer=False,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return self.harness

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def subprocess_pid(self) -> int | None:
        return self._scope_snapshot.root_pid if self._scope_snapshot is not None else None

    @property
    def primary_event_scope(self) -> PrimaryEventScope | None:
        if self.harness is HarnessId.CODEX:
            return codex_primary_event_scope(self._session_id)
        if self.harness is HarnessId.OPENCODE:
            return opencode_primary_event_scope(self._session_id)
        return None

    @property
    def resident_backend(self) -> object:
        return self._resident_backend

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        return self._scope_snapshot

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        self._project_root = config.control_root
        current_scope = self._scope_snapshot
        assert current_scope is not None
        self._scope_snapshot = ProcessScopeSnapshot(
            scope_id=current_scope.scope_id,
            owner_policy=current_scope.owner_policy,
            owner_id=str(config.spawn_id),
            role=current_scope.role,
            containment=current_scope.containment,
            root_pid=current_scope.root_pid,
            root_created_at_epoch=current_scope.root_created_at_epoch,
            pgid=current_scope.pgid,
            job_name=current_scope.job_name,
            degraded_reason=current_scope.degraded_reason,
        )
        self.state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        scope = self._scope_snapshot
        if scope is not None:
            process_scope_fallback.terminate_tree_sync(
                pid=scope.root_pid,
                created_at_epoch=scope.root_created_at_epoch,
                grace_secs=5.0,
                reason="stop_called",
                scope_id=scope.scope_id,
            )
        self.state = "stopped"
        self._scope_snapshot = None
        return StopResult()

    def health(self) -> bool:
        return self.state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        project_root = self._project_root
        assert project_root is not None
        spawn_dir = resolve_spawn_log_dir(project_root, self._spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        (spawn_dir / "report.md").write_text("# Done\n\nOpenCode completed.\n", encoding="utf-8")
        yield HarnessEvent(
            event_type=self.terminal_event_type,
            harness_id=self.harness.value,
            payload={
                "type": self.terminal_event_type,
                self.terminal_payload_session_key: self._session_id,
            },
        )


class _CodexTerminalWithScopeConnection(_OpenCodeTerminalWithScopeConnection):
    harness = HarnessId.CODEX
    terminal_event_type = "turn/completed"
    terminal_payload_session_key = "threadId"


class _ResidentDeadlineConnection:
    starts = 0

    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._session_id = "thread-resident-deadline"
        self._resident_backend = self
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=True,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CODEX

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def subprocess_pid(self) -> int | None:
        return 8282

    @property
    def primary_event_scope(self) -> None:
        return None

    @property
    def resident_backend(self) -> object:
        return self._resident_backend

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        type(self).starts += 1
        self._spawn_id = config.spawn_id
        self.state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        self.state = "stopped"
        return StopResult()

    def health(self) -> bool:
        return self.state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    def health_status(self) -> object:
        return "continue"

    def set_awaiting_done(self, awaiting: bool) -> None:
        _ = awaiting

    async def begin_followup_turn(self, message: str) -> None:
        _ = message

    async def events(self):  # type: ignore[no-untyped-def]
        yield HarnessEvent(
            event_type="turn/completed",
            harness_id="codex",
            payload={"threadId": self._session_id, "turnId": "turn-1"},
        )
        while True:
            await asyncio.sleep(3600)


class _ResidentRearmRetryConnection(_ResidentDeadlineConnection):
    runtime_root = Path()

    @classmethod
    def reset(cls, runtime_root: Path) -> None:
        cls.starts = 0
        cls.runtime_root = runtime_root

    def __init__(self) -> None:
        super().__init__()
        self._attempt_index = 0
        self._project_root: Path | None = None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        await super().start(config, spec)
        self._attempt_index = type(self).starts
        self._project_root = config.control_root

    async def events(self):  # type: ignore[no-untyped-def]
        project_root = self._project_root
        assert project_root is not None
        spawn_dir = resolve_spawn_log_dir(project_root, self._spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        (spawn_dir / "report.md").write_text(
            "# Done\n\nResident retry completed.\n",
            encoding="utf-8",
        )
        write_spawn_signal(type(self).runtime_root, self._spawn_id, "rearm")
        if self._attempt_index == 1:
            write_spawn_signal(type(self).runtime_root, self._spawn_id, "done")
        yield HarnessEvent(
            event_type="turn/completed",
            harness_id="codex",
            payload={"threadId": self._session_id, "turnId": f"turn-{self._attempt_index}"},
        )


class _ScriptedRetryOpenCodeConnection:
    starts = 0
    first_attempt_events: tuple[HarnessEvent, ...] = ()
    session_id_value = "session-scripted-retry-opencode"
    subprocess_pid_value = 8383

    @classmethod
    def reset(
        cls,
        *,
        first_attempt_events: tuple[HarnessEvent, ...],
        session_id: str,
        subprocess_pid: int,
    ) -> None:
        cls.starts = 0
        cls.first_attempt_events = first_attempt_events
        cls.session_id_value = session_id
        cls.subprocess_pid_value = subprocess_pid

    def __init__(self) -> None:
        self.state = "created"
        self._spawn_id = SpawnId("")
        self._attempt_index = 0
        self._resident_backend = _IdleResidentBackend()
        self.capabilities = ConnectionCapabilities(
            mid_turn_injection="http_post",
            supports_steer=False,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=True,
        )

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.OPENCODE

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def session_id(self) -> str | None:
        return type(self).session_id_value

    @property
    def subprocess_pid(self) -> int | None:
        return type(self).subprocess_pid_value

    @property
    def primary_event_scope(self) -> PrimaryEventScope | None:
        return opencode_primary_event_scope(type(self).session_id_value)

    @property
    def resident_backend(self) -> object:
        return self._resident_backend

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        type(self).starts += 1
        self._attempt_index = type(self).starts
        self._spawn_id = config.spawn_id
        self.state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        self.state = "stopped"
        return StopResult()

    def health(self) -> bool:
        return self.state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        if self._attempt_index == 1:
            for event in type(self).first_attempt_events:
                yield event
            return
        yield HarnessEvent(
            event_type="session.idle",
            harness_id="opencode",
            payload={"type": "session.idle", "sessionID": self.session_id},
        )


class _EndMonotonicFailsClock(FakeClock):
    def __init__(self, start: float = 0.0) -> None:
        super().__init__(start=start)
        self._monotonic_reads = 0

    def monotonic(self) -> float:
        self._monotonic_reads += 1
        if self._monotonic_reads >= 2:
            raise RuntimeError("end monotonic unavailable")
        return super().monotonic()




def _build_request() -> SpawnRequest:
    return SpawnRequest(
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX.value,
        prompt="hello",
    )


def _build_opencode_request() -> SpawnRequest:
    return SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.OPENCODE.value,
        prompt="hello",
    )


async def _execute_with_context(
    run: Spawn,
    *,
    request: SpawnRequest,
    project_root: Path,
    runtime_root: Path,
    artifacts: LocalStore,
    registry: HarnessRegistry,
    execution_cwd: Path | None = None,
    **kwargs: object,
) -> int:
    resolved_execution_cwd = execution_cwd or project_root.resolve()
    launch_context = build_launch_context(
        spawn_id=str(run.spawn_id),
        request=request,
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.SPEC_ONLY,
            runtime_root=runtime_root.as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=resolved_execution_cwd.as_posix(),
        ),
        harness_registry=registry,
    )
    return await streaming_runner_module.execute_with_streaming(
        run,
        request=request,
        launch_context=launch_context,
        project_root=project_root,
        runtime_root=runtime_root,
        artifacts=artifacts,
        **kwargs,
    )
