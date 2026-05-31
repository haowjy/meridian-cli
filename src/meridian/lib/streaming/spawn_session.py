"""Live spawn session data structures."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.harness.connections.base import HarnessEvent

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection
    from meridian.lib.harness.control_action import ControlActionCoordinator
    from meridian.lib.observability.debug_tracer import DebugTracer
    from meridian.lib.streaming.control_socket import ControlSocketServer


@dataclass(frozen=True)
class DrainOutcome:
    """Terminal drain result for one spawn session."""

    status: SpawnStatus
    exit_code: int
    error: str | None = None
    duration_secs: float = 0.0


@dataclass
class SpawnSession:
    """Live resources associated with one running spawn."""

    connection: HarnessConnection[Any]
    drain_task: asyncio.Task[None]
    subscriber: asyncio.Queue[HarnessEvent | None] | None
    control_server: ControlSocketServer
    started_monotonic: float
    completion_future: asyncio.Future[DrainOutcome]
    debug_tracer: DebugTracer | None = None
    cancel_sent: bool = False
    cancel_event_emitted: bool = False
    control_actions: ControlActionCoordinator | None = None
