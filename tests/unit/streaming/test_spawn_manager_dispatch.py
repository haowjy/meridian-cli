"""dispatch_start transport selection tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.types import HarnessId, SpawnId, TransportId
from meridian.lib.harness.connections.base import (
    ConnectionCapabilities,
    ConnectionConfig,
    ConnectionState,
    HarnessConnection,
    HarnessEvent,
    StopProgressCallback,
    StopResult,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.streaming.spawn_manager import dispatch_start


class _FakeConnection(HarnessConnection[ResolvedLaunchSpec]):
    def __init__(self) -> None:
        self.started = False
        self._spawn_id = SpawnId("")
        self._state: ConnectionState = "created"

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def harness_id(self) -> HarnessId:
        return HarnessId.CURSOR

    @property
    def spawn_id(self) -> SpawnId:
        return self._spawn_id

    @property
    def capabilities(self) -> ConnectionCapabilities:
        return ConnectionCapabilities(
            mid_turn_injection="queue",
            supports_steer=False,
            supports_cancel=True,
            runtime_model_switch=False,
            structured_reasoning=False,
        )

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def subprocess_pid(self) -> int | None:
        return None

    async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
        _ = spec
        self._spawn_id = config.spawn_id
        self._state = "connected"
        self.started = True

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = reason, progress
        self._state = "stopped"
        return StopResult()

    def health(self) -> bool:
        return self._state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self) -> AsyncIterator[HarnessEvent]:
        if False:
            yield HarnessEvent(event_type="never", harness_id="cursor", payload={})


@pytest.mark.asyncio
async def test_dispatch_start_uses_contract_transport_for_cursor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    selected: dict[str, Any] = {}

    def _fake_get_connection_class(
        harness_id: HarnessId,
        transport_id: TransportId = TransportId.STREAMING,
    ) -> type[HarnessConnection[Any]]:
        selected["harness_id"] = harness_id
        selected["transport_id"] = transport_id
        return _FakeConnection

    monkeypatch.setattr(
        "meridian.lib.harness.connections.get_connection_class",
        _fake_get_connection_class,
    )

    config = ConnectionConfig(
        spawn_id=SpawnId("spawn-cursor-dispatch"),
        harness_id=HarnessId.CURSOR,
        prompt="Reply with exactly OK",
        control_root=tmp_path,
        env_overrides={},
    )
    spec = ResolvedLaunchSpec(
        harness=HarnessId.CURSOR.value,
        prompt="Reply with exactly OK",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    connection = await dispatch_start(config, spec)

    assert isinstance(connection, _FakeConnection)
    assert connection.started is True
    assert selected == {
        "harness_id": HarnessId.CURSOR,
        "transport_id": TransportId.SUBPROCESS,
    }
