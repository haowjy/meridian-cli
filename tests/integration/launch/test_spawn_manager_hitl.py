# qa-validated: test-suite-redesign
"""SpawnManager HITL tests: auto-reject for spawned agents and retryable send failure."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from meridian.lib.core.types import HarnessId, SpawnId, TransportId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    HarnessEvent,
    HarnessRequest,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.paths import resolve_runtime_paths
from meridian.lib.state.spawn_store import start_spawn
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from meridian.lib.streaming.spawn_manager import SpawnManager


def _build_config(spawn_id: SpawnId, project_root: Path) -> ConnectionConfig:
    return ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=HarnessId.CODEX,
        prompt="hello",
        control_root=project_root,
        env_overrides={},
    )


def _build_spec() -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )


def _read_permission_statuses(runtime_root: Path, spawn_id: SpawnId) -> list[str]:
    journal_path = runtime_root / "spawns" / str(spawn_id) / "permission_requests.jsonl"
    if not journal_path.exists():
        return []
    statuses: list[str] = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = cast("dict[str, object]", json.loads(line))
        status = payload.get("status")
        if isinstance(status, str):
            statuses.append(status)
    return statuses


@pytest.mark.asyncio
async def test_spawn_manager_codex_hitl_requests_auto_rejected_for_spawned_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir

    class FakeControlSocketServer:
        def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: SpawnManager) -> None:
            _ = spawn_id, manager
            self.socket_path = socket_path

        async def start(self) -> None:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        async def stop(self) -> None:
            return None

    class FakeConnection:
        def __init__(self, request_handler: object | None = None) -> None:
            self._spawn_id = SpawnId("")
            self._request_handler = request_handler
            self._events: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
            self.state = "created"
            self.respond_calls: list[tuple[str, str, dict[str, object] | None]] = []

        @property
        def capabilities(self) -> ConnectionCapabilities:
            return ConnectionCapabilities(
                mid_turn_injection="interrupt_restart",
                supports_steer=True,
                supports_cancel=True,
                runtime_model_switch=False,
                structured_reasoning=True,
                supports_runtime_hitl=not getattr(self._request_handler, "no_runtime_hitl", True),
            )

        @property
        def harness_id(self) -> HarnessId:
            return HarnessId.CODEX

        @property
        def spawn_id(self) -> SpawnId:
            return self._spawn_id

        @property
        def session_id(self) -> str | None:
            return None

        @property
        def subprocess_pid(self) -> int | None:
            return None

        @property
        def primary_event_scope(self) -> None:
            return None

        @property
        def resident_backend(self) -> None:
            return None

        async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
            _ = spec
            self._spawn_id = config.spawn_id
            self.state = "connected"
            if self._request_handler is None:
                raise AssertionError("expected runtime HITL request handler")
            await cast("Any", self._request_handler).handle_request(
                self,
                HarnessRequest(
                    request_id="approval-1",
                    request_type="approval",
                    method="item/commandExecution/requestApproval",
                    payload={"command": "echo hi"},
                ),
            )

        async def stop(self) -> None:
            self.state = "stopped"
            await self._events.put(None)

        def health(self) -> bool:
            return self.state == "connected"

        async def send_user_message(self, text: str) -> None:
            _ = text

        async def send_cancel(self) -> None:
            return None

        async def inject_runtime_event(self, event: HarnessEvent) -> None:
            await self._events.put(event)

        async def respond_request(
            self,
            request_id: str,
            decision: str,
            payload: dict[str, object] | None = None,
        ) -> None:
            self.respond_calls.append((request_id, decision, payload))
            callback = getattr(self._request_handler, "on_request_resolved", None)
            if callback is not None:
                await cast("Any", callback)(request_id, resolution={"decision": decision})

        async def respond_user_input(self, request_id: str, answers: dict[str, object]) -> None:
            _ = request_id, answers

        async def events(self):  # type: ignore[no-untyped-def]
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: FakeConnection,
    )

    spawn_id = start_spawn(
        runtime_root,
        chat_id="c-hitl",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        kind="streaming",
        prompt="hello",
        launch_mode="foreground",
        status="running",
    )
    manager = SpawnManager(runtime_root=runtime_root, project_root=project_root)
    await manager.start_spawn(_build_config(spawn_id, project_root), _build_spec())

    try:
        connection = cast("Any", manager.get_connection(spawn_id))
        assert connection is not None
        assert connection.capabilities.supports_runtime_hitl is True

        # auto_reject_runtime_requests=True causes immediate rejection during start
        assert connection.respond_calls == [("approval-1", "reject", None)]
        assert _read_permission_statuses(runtime_root, spawn_id) == ["pending", "resolved"]
    finally:
        await manager.stop_spawn(spawn_id)


@pytest.mark.asyncio
async def test_spawn_manager_retryable_permission_send_failure_resolves_not_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path
    runtime_root = resolve_runtime_paths(project_root).root_dir

    class FakeControlSocketServer:
        def __init__(self, spawn_id: SpawnId, socket_path: Path, manager: SpawnManager) -> None:
            _ = spawn_id, manager
            self.socket_path = socket_path

        async def start(self) -> None:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        async def stop(self) -> None:
            return None

    class FakeConnection:
        def __init__(self, request_handler: object | None = None) -> None:
            self._spawn_id = SpawnId("")
            self._request_handler = request_handler
            self._events: asyncio.Queue[HarnessEvent | None] = asyncio.Queue()
            self.state = "created"
            self.respond_attempts = 0

        @property
        def capabilities(self) -> ConnectionCapabilities:
            return ConnectionCapabilities(
                mid_turn_injection="interrupt_restart",
                supports_steer=True,
                supports_cancel=True,
                runtime_model_switch=False,
                structured_reasoning=True,
                supports_runtime_hitl=not getattr(self._request_handler, "no_runtime_hitl", True),
            )

        @property
        def harness_id(self) -> HarnessId:
            return HarnessId.CODEX

        @property
        def spawn_id(self) -> SpawnId:
            return self._spawn_id

        @property
        def session_id(self) -> str | None:
            return None

        @property
        def subprocess_pid(self) -> int | None:
            return None

        @property
        def primary_event_scope(self) -> None:
            return None

        @property
        def resident_backend(self) -> None:
            return None

        async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
            _ = spec
            self._spawn_id = config.spawn_id
            self.state = "connected"
            if self._request_handler is None:
                raise AssertionError("expected runtime HITL request handler")
            await cast("Any", self._request_handler).handle_request(
                self,
                HarnessRequest(
                    request_id="approval-1",
                    request_type="approval",
                    method="item/commandExecution/requestApproval",
                    payload={"command": "echo hi"},
                ),
            )

        async def stop(self) -> None:
            self.state = "stopped"
            await self._events.put(None)

        def health(self) -> bool:
            return self.state == "connected"

        async def send_user_message(self, text: str) -> None:
            _ = text

        async def send_cancel(self) -> None:
            return None

        async def inject_runtime_event(self, event: HarnessEvent) -> None:
            await self._events.put(event)

        async def respond_request(
            self,
            request_id: str,
            decision: str,
            payload: dict[str, object] | None = None,
        ) -> None:
            _ = payload
            self.respond_attempts += 1
            # Attempt 1 is the auto-reject from PermissionBroker (succeeds).
            # Attempt 2 is the manager's first explicit call (fails, triggering retry).
            # Attempt 3 is the coordinator's retry (succeeds).
            if self.respond_attempts == 2:
                raise ConnectionError("retryable send failure")
            callback = getattr(self._request_handler, "on_request_resolved", None)
            if callback is not None:
                await cast("Any", callback)(request_id, resolution={"decision": decision})

        async def respond_user_input(self, request_id: str, answers: dict[str, object]) -> None:
            _ = request_id, answers

        async def events(self):  # type: ignore[no-untyped-def]
            while True:
                event = await self._events.get()
                if event is None:
                    return
                yield event

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", FakeControlSocketServer)
    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        lambda _harness_id, _transport_id=TransportId.STREAMING: FakeConnection,
    )

    spawn_id = start_spawn(
        runtime_root,
        chat_id="c-hitl-retry",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        kind="streaming",
        prompt="hello",
        launch_mode="foreground",
        status="running",
    )
    manager = SpawnManager(runtime_root=runtime_root, project_root=project_root)
    await manager.start_spawn(_build_config(spawn_id, project_root), _build_spec())

    try:
        await manager.respond_request(
            spawn_id,
            request_id="approval-1",
            decision="accept",
            source="test",
        )
        connection = cast("Any", manager.get_connection(spawn_id))
        assert connection is not None
        assert connection.respond_attempts == 3
        assert _read_permission_statuses(runtime_root, spawn_id) == ["pending", "resolved"]
    finally:
        await manager.stop_spawn(spawn_id)
