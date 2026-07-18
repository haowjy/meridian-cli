"""Live spawn session data structures."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meridian.lib.core.domain import SpawnStatus

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import HarnessConnection
    from meridian.lib.harness.control_action import ControlActionCoordinator
    from meridian.lib.harness.semantics import NormalizedHarnessEvent
    from meridian.lib.observability.debug_tracer import DebugTracer
    from meridian.lib.streaming.control_socket import ControlSocketServer
    from meridian.lib.streaming.drain_coordinator import DrainPlan
    from meridian.lib.streaming.drain_teardown import DrainSessionTeardown


@dataclass(frozen=True)
class DrainOutcome:
    """Terminal drain result for one spawn session."""

    status: SpawnStatus
    exit_code: int
    error: str | None = None
    duration_secs: float = 0.0
    authoritative: bool = False


@dataclass
class SpawnSession:
    """Live resources associated with one running spawn."""

    connection: HarnessConnection[Any]
    drain_task: asyncio.Task[None]
    subscriber: asyncio.Queue[NormalizedHarnessEvent | None] | None
    control_server: ControlSocketServer
    started_monotonic: float
    completion_future: asyncio.Future[DrainOutcome]
    raw_terminal_frames_authoritative: bool
    control_actions: ControlActionCoordinator
    teardown: DrainSessionTeardown
    drain_plan: DrainPlan
    debug_tracer: DebugTracer | None = None
    cancel_sent: bool = False
    cancel_event_emitted: bool = False
    authoritative_stop_outcome: DrainOutcome | None = None
    terminal_published: bool = False
    cleanup_task: asyncio.Task[None] | None = None
