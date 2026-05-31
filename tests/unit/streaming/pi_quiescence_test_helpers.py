"""Shared test helpers for Pi quiescence suites."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.types import HarnessId, SpawnId
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
from meridian.lib.streaming.control_socket import ControlSocketServer


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
        _ = spec
        self._spawn_id = config.spawn_id
        self._state = "connected"

    async def stop(
        self,
        *,
        reason: str | None = None,
        progress: StopProgressCallback | None = None,
    ) -> StopResult:
        _ = progress
        self.stop_reasons.append(reason)
        self._state = "stopped"
        return StopResult()

    def health(self) -> bool:
        return self._state == "connected"

    async def send_user_message(self, text: str) -> None:
        _ = text

    async def send_cancel(self) -> None:
        return None

    async def events(self):  # type: ignore[no-untyped-def]
        for event in self._events:
            yield event


def pi_event(event_type: str, payload: dict[str, object]) -> HarnessEvent:
    return HarnessEvent(event_type=event_type, harness_id="pi", payload=payload)
