"""Runtime registry and durable drain for active harness connections."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import psutil

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.control_action import (
    ControlActionCoordinator,
    ControlActionType,
)
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.harness.permission_broker import PermissionBroker
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state import spawn_store
from meridian.lib.state.atomic import append_text_line
from meridian.lib.state.history import HarnessHistoryWriter
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.streaming.control_socket import ControlSocketServer
from meridian.lib.streaming.drain_policy import (
    TURN_BOUNDARY_EVENT_TYPE,
    DrainAction,
    DrainPolicy,
    PiRpcQuiescenceDrainPolicy,
    SingleTurnDrainPolicy,
)
from meridian.lib.streaming.event_observers import (
    CallbackObserver,
    EventObserver,
    EventObserverRegistry,
    HarnessEventCallback,
)
from meridian.lib.streaming.heartbeat import heartbeat_loop
from meridian.lib.streaming.types import InjectResult

if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import (
        ConnectionConfig,
        HarnessConnection,
    )
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.observability.debug_tracer import DebugTracer

logger = logging.getLogger(__name__)
InjectResultCallback = Callable[[InjectResult], None]

_PI_SUBSPAWN_START_EVENTS: frozenset[str] = frozenset(
    {
        "meridian_subspawn_start",
        "meridian.subspawn.start",
    }
)
_PI_CANONICAL_SUBSPAWN_START_EVENTS: frozenset[str] = frozenset({"meridian.subspawn.start"})
_PI_LEGACY_SUBSPAWN_START_EVENTS: frozenset[str] = frozenset({"meridian_subspawn_start"})
_PI_SUBSPAWN_END_EVENTS: frozenset[str] = frozenset(
    {
        "meridian_subspawn_end",
        "meridian.subspawn.end",
    }
)
_PI_CANONICAL_SUBSPAWN_END_EVENTS: frozenset[str] = frozenset({"meridian.subspawn.end"})
_PI_LEGACY_SUBSPAWN_END_EVENTS: frozenset[str] = frozenset({"meridian_subspawn_end"})
_PI_NOTIFICATION_QUEUED_EVENTS: frozenset[str] = frozenset(
    {"meridian.notification.queued", "meridian_notification_queued"}
)
_PI_NOTIFICATION_DELIVERED_EVENTS: frozenset[str] = frozenset(
    {"meridian.notification.delivered", "meridian_notification_delivered"}
)
_PI_NOTIFICATION_COMPLETED_EVENTS: frozenset[str] = frozenset(
    {"meridian.notification.completed", "meridian_notification_completed"}
)
_PI_NOTIFICATION_FAILED_EVENTS: frozenset[str] = frozenset(
    {"meridian.notification.failed", "meridian_notification_failed"}
)
_PI_CANONICAL_NOTIFICATION_EVENTS: frozenset[str] = frozenset(
    {
        "meridian.notification.queued",
        "meridian.notification.delivered",
        "meridian.notification.completed",
        "meridian.notification.failed",
    }
)
_PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION = 1
_PI_CANONICAL_LIFECYCLE_EVENT_PREFIXES: tuple[str, ...] = (
    "meridian.subspawn.",
    "meridian.notification.",
    "meridian.quiescence.",
)
_PI_QUIESCENCE_IDLE_GRACE_SECONDS: float = 2.0

StartConnectionPort = Callable[
    ["ConnectionConfig", ResolvedLaunchSpec],
    Awaitable["HarnessConnection[Any]"],
]
ControlServerFactory = Callable[[SpawnId, Path, "SpawnManager"], ControlSocketServer]


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


def _normalize_label(raw: str) -> str:
    return raw.strip().lower().replace("-", "_").replace("/", ".")


def _event_label_candidates(event: HarnessEvent) -> tuple[str, ...]:
    candidates: list[str] = []
    event_type = _normalize_label(event.event_type)
    if event_type:
        candidates.append(event_type)
    payload_type = event.payload.get("type")
    if isinstance(payload_type, str):
        payload_label = _normalize_label(payload_type)
        if payload_label and payload_label not in candidates:
            candidates.append(payload_label)
    return tuple(candidates)


def _is_pi_lifecycle_event(event: HarnessEvent) -> bool:
    labels = set(_event_label_candidates(event))
    if "meridian.lifecycle.parse_error" in labels:
        return True
    if labels & (
        _PI_SUBSPAWN_START_EVENTS
        | _PI_SUBSPAWN_END_EVENTS
        | _PI_NOTIFICATION_QUEUED_EVENTS
        | _PI_NOTIFICATION_DELIVERED_EVENTS
        | _PI_NOTIFICATION_COMPLETED_EVENTS
        | _PI_NOTIFICATION_FAILED_EVENTS
    ):
        return True
    return any(label.startswith(_PI_CANONICAL_LIFECYCLE_EVENT_PREFIXES) for label in labels)


def _is_legacy_notification_label(labels: set[str]) -> bool:
    if labels & _PI_CANONICAL_NOTIFICATION_EVENTS:
        return False
    return any(
        label.startswith("meridian_notification_")
        and label not in _PI_CANONICAL_NOTIFICATION_EVENTS
        for label in labels
    )


def _pi_subspawn_id(payload: dict[str, object]) -> str | None:
    for key in ("subspawn_id", "spawn_id", "child_spawn_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _pi_notification_id(payload: dict[str, object]) -> str | None:
    for key in ("notification_id", "correlation_id", "id"):
        value = payload.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _pi_wait_policy_is_tracked(payload: dict[str, object]) -> bool:
    raw_policy = payload.get("wait_policy")
    if not isinstance(raw_policy, str):
        return True
    return raw_policy.strip().lower() != "detached"


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.startswith(("+", "-")):
            sign = raw[0]
            digits = raw[1:]
            if digits.isdigit():
                return int(f"{sign}{digits}")
            return None
        if raw.isdigit():
            return int(raw)
    return None


def _pi_subspawn_pid(payload: dict[str, object]) -> int | None:
    for key in ("pid", "child_pid", "process_id"):
        value = _coerce_int(payload.get(key))
        if value is not None:
            return value
    return None


def _pi_subspawn_pgid(payload: dict[str, object]) -> int | None:
    for key in ("pgid", "process_group_id"):
        value = _coerce_int(payload.get(key))
        if value is not None:
            return value
    return None


def _pi_notification_failure_error(payload: dict[str, object]) -> str:
    reason = payload.get("reason")
    error = payload.get("error")
    reason_text = reason.strip() if isinstance(reason, str) else ""
    error_text = error.strip() if isinstance(error, str) else ""
    if reason_text and error_text:
        return f"pi_notification_failed:{reason_text}:{error_text}"
    if reason_text:
        return f"pi_notification_failed:{reason_text}"
    if error_text:
        return f"pi_notification_failed:{error_text}"
    return "pi_notification_failed"


def _unsupported_pi_schema_version_error(
    labels: set[str],
    payload: dict[str, object],
) -> str | None:
    canonical_lifecycle_labels = {
        label
        for label in labels
        if label.startswith(_PI_CANONICAL_LIFECYCLE_EVENT_PREFIXES)
    }
    if not canonical_lifecycle_labels:
        return None

    raw_schema_version = payload.get("schema_version")
    if raw_schema_version is None:
        return None
    schema_version = _coerce_int(raw_schema_version)
    if schema_version is None:
        return "pi_lifecycle_tracking_invalidated:unsupported_schema_version:unknown"
    if schema_version != _PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION:
        return (
            "pi_lifecycle_tracking_invalidated:unsupported_schema_version:"
            f"{schema_version}"
        )
    return None


def _canonical_lifecycle_label(
    labels: set[str],
    canonical_labels: frozenset[str],
) -> str:
    matched = sorted(label for label in labels if label in canonical_labels)
    if matched:
        return matched[0]
    return "unknown"


@dataclass
class _PiSubspawnTracker:
    active_ids: set[str]
    active_process_groups: dict[str, int]
    anonymous_active_count: int = 0
    pending_notification_ids: set[str] | None = None
    anonymous_pending_notifications: int = 0
    notification_failure_error: str | None = None
    lifecycle_tracking_invalidated_error: str | None = None

    @classmethod
    def empty(cls) -> _PiSubspawnTracker:
        return cls(active_ids=set(), active_process_groups={}, pending_notification_ids=set())

    def observe(self, event: HarnessEvent) -> None:
        if event.harness_id != HarnessId.PI.value:
            return

        labels = _event_label_candidates(event)
        label_set = set(labels)
        lifecycle_schema_error = _unsupported_pi_schema_version_error(label_set, event.payload)
        if lifecycle_schema_error is not None:
            self.lifecycle_tracking_invalidated_error = lifecycle_schema_error
            return
        if self._is_parse_error_for_canonical_lifecycle_event(event):
            raw_type = event.payload.get("raw_type")
            raw_type_text = raw_type if isinstance(raw_type, str) else "unknown"
            self.lifecycle_tracking_invalidated_error = (
                "pi_lifecycle_tracking_invalidated:"
                f"unsupported_schema_event:{raw_type_text}"
            )
            return

        is_subspawn_start = bool(label_set & _PI_SUBSPAWN_START_EVENTS)
        if is_subspawn_start:
            if not _pi_wait_policy_is_tracked(event.payload):
                return
            has_canonical_label = bool(label_set & _PI_CANONICAL_SUBSPAWN_START_EVENTS)
            has_legacy_label = bool(label_set & _PI_LEGACY_SUBSPAWN_START_EVENTS)
            subspawn_id = _pi_subspawn_id(event.payload)
            if subspawn_id is not None:
                self.active_ids.add(subspawn_id)
                pgid = _pi_subspawn_pgid(event.payload)
                pid = _pi_subspawn_pid(event.payload)
                process_group_id = pgid if pgid is not None else pid
                if process_group_id is not None:
                    self.active_process_groups[subspawn_id] = process_group_id
            elif has_canonical_label:
                canonical_label = _canonical_lifecycle_label(
                    label_set,
                    _PI_CANONICAL_SUBSPAWN_START_EVENTS,
                )
                self.lifecycle_tracking_invalidated_error = (
                    "pi_lifecycle_tracking_invalidated:"
                    f"missing_subspawn_id:{canonical_label}"
                )
            elif has_legacy_label and not has_canonical_label:
                self.anonymous_active_count += 1
            return

        is_subspawn_end = bool(label_set & _PI_SUBSPAWN_END_EVENTS)
        if is_subspawn_end:
            if not _pi_wait_policy_is_tracked(event.payload):
                return
            has_canonical_label = bool(label_set & _PI_CANONICAL_SUBSPAWN_END_EVENTS)
            has_legacy_label = bool(label_set & _PI_LEGACY_SUBSPAWN_END_EVENTS)
            subspawn_id = _pi_subspawn_id(event.payload)
            if subspawn_id is not None:
                self.active_ids.discard(subspawn_id)
                self.active_process_groups.pop(subspawn_id, None)
                return
            if has_canonical_label:
                canonical_label = _canonical_lifecycle_label(
                    label_set,
                    _PI_CANONICAL_SUBSPAWN_END_EVENTS,
                )
                self.lifecycle_tracking_invalidated_error = (
                    "pi_lifecycle_tracking_invalidated:"
                    f"missing_subspawn_id:{canonical_label}"
                )
                return
            if has_legacy_label and not has_canonical_label and self.anonymous_active_count > 0:
                self.anonymous_active_count -= 1
            return

        pending_notification_ids = self.pending_notification_ids
        if pending_notification_ids is None:
            return

        is_notification_start = bool(
            label_set & (_PI_NOTIFICATION_QUEUED_EVENTS | _PI_NOTIFICATION_DELIVERED_EVENTS)
        )
        if is_notification_start:
            notification_id = _pi_notification_id(event.payload)
            if notification_id is not None:
                pending_notification_ids.add(notification_id)
            elif label_set & _PI_CANONICAL_NOTIFICATION_EVENTS:
                canonical_label = _canonical_lifecycle_label(
                    label_set,
                    _PI_CANONICAL_NOTIFICATION_EVENTS,
                )
                self.lifecycle_tracking_invalidated_error = (
                    "pi_lifecycle_tracking_invalidated:"
                    f"missing_notification_id:{canonical_label}"
                )
            elif _is_legacy_notification_label(label_set):
                self.anonymous_pending_notifications += 1
            return

        is_notification_end = bool(
            label_set & (_PI_NOTIFICATION_COMPLETED_EVENTS | _PI_NOTIFICATION_FAILED_EVENTS)
        )
        if is_notification_end:
            notification_id = _pi_notification_id(event.payload)
            if notification_id is not None:
                pending_notification_ids.discard(notification_id)
            elif label_set & _PI_CANONICAL_NOTIFICATION_EVENTS:
                canonical_label = _canonical_lifecycle_label(
                    label_set,
                    _PI_CANONICAL_NOTIFICATION_EVENTS,
                )
                self.lifecycle_tracking_invalidated_error = (
                    "pi_lifecycle_tracking_invalidated:"
                    f"missing_notification_id:{canonical_label}"
                )
                return
            elif (
                _is_legacy_notification_label(label_set)
                and self.anonymous_pending_notifications > 0
            ):
                self.anonymous_pending_notifications -= 1
            if label_set & _PI_NOTIFICATION_FAILED_EVENTS:
                self.notification_failure_error = _pi_notification_failure_error(event.payload)

    def _is_parse_error_for_canonical_lifecycle_event(self, event: HarnessEvent) -> bool:
        labels = _event_label_candidates(event)
        label_set = set(labels)
        if "meridian.lifecycle.parse_error" not in label_set:
            return False
        raw_type = event.payload.get("raw_type")
        if not isinstance(raw_type, str):
            return False
        if not raw_type.startswith(_PI_CANONICAL_LIFECYCLE_EVENT_PREFIXES):
            return False
        parse_error = event.payload.get("error")
        return isinstance(parse_error, str) and parse_error == "unsupported_schema_version"

    def has_pending(self) -> bool:
        return bool(self.active_ids) or self.anonymous_active_count > 0

    def active_tracked_pgid_candidates(self) -> tuple[int, ...]:
        unique = {
            pgid
            for subspawn_id, pgid in self.active_process_groups.items()
            if subspawn_id in self.active_ids and pgid > 0
        }
        return tuple(sorted(unique))

    def has_pending_notifications(self) -> bool:
        pending_notification_ids = self.pending_notification_ids
        return bool(pending_notification_ids) or self.anonymous_pending_notifications > 0


def _ensure_harness_bootstrap() -> None:
    from meridian.lib.harness import ensure_bootstrap

    ensure_bootstrap()


async def dispatch_start(
    config: ConnectionConfig,
    spec: ResolvedLaunchSpec,
) -> HarnessConnection[Any]:
    """Dispatch one start call through bundle lookup and runtime type guard."""

    from meridian.lib.harness.connections import get_connection_class

    _ensure_harness_bootstrap()
    bundle = get_harness_bundle(config.harness_id)
    if not isinstance(spec, bundle.spec_cls):
        raise TypeError(
            f"HarnessBundle invariant violated: adapter for "
            f"{bundle.harness_id} returned {type(spec).__name__}, "
            f"expected {bundle.spec_cls.__name__}"
        )

    connection_class = get_connection_class(config.harness_id)
    request_handler: PermissionBroker | None = None
    connection_ref: dict[str, HarnessConnection[Any]] = {}

    async def _runtime_event_sink(event: HarnessEvent) -> None:
        await connection_ref["connection"].inject_runtime_event(event)

    if config.harness_id is HarnessId.CODEX:
        request_handler = PermissionBroker(
            spawn_dir=resolve_spawn_log_dir(config.control_root, config.spawn_id),
            event_sink=_runtime_event_sink,
            auto_reject_runtime_requests=True,
        )

    connection: HarnessConnection[Any]
    if request_handler is not None:
        connection_factory = cast(
            "Callable[..., HarnessConnection[Any]]",
            connection_class,
        )
        try:
            connection = connection_factory(request_handler=request_handler)
        except TypeError:
            connection = cast("Callable[[], HarnessConnection[Any]]", connection_class)()
    else:
        connection = cast("Callable[[], HarnessConnection[Any]]", connection_class)()

    connection_ref["connection"] = connection

    try:
        await connection.start(config, spec)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise HarnessBinaryNotFound.from_os_error(
            harness_id=config.harness_id,
            error=exc,
        ) from exc
    return connection


def _default_control_server_factory(
    spawn_id: SpawnId,
    socket_path: Path,
    manager: SpawnManager,
) -> ControlSocketServer:
    return ControlSocketServer(
        spawn_id=spawn_id,
        socket_path=socket_path,
        manager=manager,
    )


class SpawnManager:
    """Own active connections, durable drain loops, and control routing."""

    def __init__(
        self,
        runtime_root: Path,
        project_root: Path,
        *,
        debug: bool = False,
        heartbeat_interval_secs: float = 30.0,
        pi_quiescence_idle_grace_secs: float = _PI_QUIESCENCE_IDLE_GRACE_SECONDS,
        heartbeat_touch: Callable[[Path, SpawnId], None] | None = None,
        start_connection: StartConnectionPort | None = None,
        control_server_factory: ControlServerFactory | None = None,
    ):
        self._runtime_root = runtime_root
        self._project_root = project_root
        self._debug = debug
        self._heartbeat_interval_secs = heartbeat_interval_secs
        self._pi_quiescence_idle_grace_secs = max(0.0, pi_quiescence_idle_grace_secs)
        self._heartbeat_touch = heartbeat_touch
        self._start_connection = start_connection or dispatch_start
        self._control_server_factory = control_server_factory or _default_control_server_factory
        self._sessions: dict[SpawnId, SpawnSession] = {}
        self._completion_futures: dict[SpawnId, asyncio.Future[DrainOutcome]] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._heartbeat_tasks: dict[SpawnId, asyncio.Task[None]] = {}
        self._history_writers: dict[SpawnId, HarnessHistoryWriter] = {}
        self._observers = EventObserverRegistry()

    @property
    def runtime_root(self) -> Path:
        """Return the resolved Meridian state root."""

        return self._runtime_root

    @property
    def project_root(self) -> Path:
        """Return the repository root used for managed spawns."""

        return self._project_root

    async def _start_heartbeat(self, spawn_id: SpawnId) -> None:
        """Start heartbeat ownership for one spawn; idempotent."""

        current_task = self._heartbeat_tasks.get(spawn_id)
        if current_task is not None and not current_task.done():
            return

        task = asyncio.create_task(
            heartbeat_loop(
                self._runtime_root,
                spawn_id,
                interval=self._heartbeat_interval_secs,
                touch=self._heartbeat_touch,
            )
        )
        self._heartbeat_tasks[spawn_id] = task

        def _drop_heartbeat(done_task: asyncio.Task[None]) -> None:
            tracked = self._heartbeat_tasks.get(spawn_id)
            if tracked is done_task:
                self._heartbeat_tasks.pop(spawn_id, None)
            with suppress(asyncio.CancelledError):
                if done_task.exception() is not None:
                    logger.warning(
                        "Heartbeat loop exited unexpectedly for spawn %s: %s",
                        spawn_id,
                        done_task.exception(),
                    )

        task.add_done_callback(_drop_heartbeat)

    async def start_heartbeat(self, spawn_id: SpawnId) -> None:
        """Start heartbeat ownership for one spawn; public observer-safe seam."""

        await self._start_heartbeat(spawn_id)

    async def _stop_heartbeat(self, spawn_id: SpawnId) -> None:
        """Stop heartbeat ownership for one spawn; idempotent."""

        task = self._heartbeat_tasks.pop(spawn_id, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def start_spawn(
        self,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
        *,
        drain_policy: DrainPolicy | None = None,
        on_event: HarnessEventCallback | None = None,
    ) -> HarnessConnection[Any]:
        """Start one connection and register durable drain/control resources."""

        spawn_id = config.spawn_id
        if spawn_id in self._sessions:
            msg = f"Spawn {spawn_id} is already active"
            raise ValueError(msg)

        started_monotonic = time.monotonic()
        completion_future: asyncio.Future[DrainOutcome] = asyncio.get_running_loop().create_future()

        tracer = config.debug_tracer
        if tracer is None and self._debug:
            from meridian.lib.observability.debug_tracer import DebugTracer as _DebugTracer

            tracer = _DebugTracer(
                spawn_id=str(spawn_id),
                debug_path=self._spawn_dir(spawn_id) / "debug.jsonl",
            )

        try:
            connection = await self._start_connection(config, spec)
        except Exception:
            if tracer is not None:
                tracer.close()
            raise

        resolved_policy = drain_policy
        pi_session_role = config.pi_session_role
        self._history_writers[spawn_id] = HarnessHistoryWriter(self._history_path(spawn_id))
        if on_event is not None:
            self.register_observer(spawn_id, CallbackObserver(on_event))
        drain_task = asyncio.create_task(
            self._drain_loop(
                spawn_id,
                connection,
                tracer,
                drain_policy=resolved_policy,
                pi_session_role=pi_session_role,
            )
        )
        control_server = self._control_server_factory(
            spawn_id,
            self._spawn_dir(spawn_id) / "control.sock",
            self,
        )

        try:
            await control_server.start()
        except Exception:
            if tracer is not None:
                tracer.close()
            await self._observers.shutdown(spawn_id)
            drain_task.cancel()
            with suppress(asyncio.CancelledError):
                await drain_task
            with suppress(Exception):
                await connection.stop()
            raise

        self._sessions[spawn_id] = SpawnSession(
            connection=connection,
            drain_task=drain_task,
            subscriber=None,
            control_server=control_server,
            started_monotonic=started_monotonic,
            completion_future=completion_future,
            debug_tracer=tracer,
            control_actions=ControlActionCoordinator(
                spawn_id=spawn_id,
                spawn_dir=self._spawn_dir(spawn_id),
            ),
        )
        self._completion_futures[spawn_id] = completion_future
        return connection

    def register_observer(self, spawn_id: SpawnId, observer: EventObserver) -> None:
        """Register a non-blocking post-persist event observer for one spawn."""

        self._observers.register(spawn_id, observer)

    def unregister_observer(self, spawn_id: SpawnId, observer: EventObserver) -> None:
        """Remove a previously registered event observer for one spawn."""

        self._observers.unregister(spawn_id, observer)

    def control_endpoint(self, spawn_id: SpawnId) -> str | None:
        """Return the current platform-aware control endpoint for one active spawn."""

        session = self._sessions.get(spawn_id)
        if session is None:
            return None
        return session.control_server.endpoint

    async def _drain_loop(
        self,
        spawn_id: SpawnId,
        receiver: HarnessConnection[Any],
        tracer: DebugTracer | None = None,
        drain_policy: DrainPolicy | None = None,
        pi_session_role: str | None = None,
    ) -> None:
        """Durably append each harness event and fan out to the active subscriber.

        Writes a stable event envelope to history.jsonl so event type and harness
        identity are preserved even when the payload itself omits that metadata.
        """

        # Import at runtime to avoid circular import during module initialization.
        from meridian.lib.harness.semantics import (
            TerminalEventOutcome,
            activity_transition,
            terminal_outcome,
        )

        consecutive_write_failures = 0
        max_consecutive_failures = 10
        drain_cancelled = False
        drain_error: Exception | None = None
        recorded_terminal_outcome: TerminalEventOutcome | None = None
        pending_terminal_outcome: TerminalEventOutcome | None = None
        last_successful_pi_terminal: TerminalEventOutcome | None = None
        pi_quiescence_deadline_monotonic: float | None = None
        pi_subspawn_tracker = _PiSubspawnTracker.empty()
        pi_tracked_cleanup_reason: str | None = None
        is_pi_connection = receiver.harness_id == HarnessId.PI
        normalized_pi_session_role = (pi_session_role or "").strip().lower()
        pi_parent_idle = False

        def _is_pi_quiescent() -> bool:
            return (
                is_pi_connection
                and normalized_pi_session_role == "spawned"
                and pi_parent_idle
                and not pi_subspawn_tracker.has_pending()
                and not pi_subspawn_tracker.has_pending_notifications()
            )

        policy = drain_policy
        if policy is None:
            if is_pi_connection:
                policy = PiRpcQuiescenceDrainPolicy(quiescence_check=_is_pi_quiescent)
            else:
                policy = SingleTurnDrainPolicy()
        pi_quiescence_enabled = is_pi_connection and isinstance(policy, PiRpcQuiescenceDrainPolicy)
        events_iter = receiver.events().__aiter__()
        try:
            while True:
                try:
                    if pi_quiescence_enabled and pi_quiescence_deadline_monotonic is not None:
                        remaining = pi_quiescence_deadline_monotonic - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        event = await asyncio.wait_for(anext(events_iter), timeout=remaining)
                    else:
                        event = await anext(events_iter)
                except StopAsyncIteration:
                    if (
                        pi_quiescence_enabled
                        and pi_quiescence_deadline_monotonic is not None
                        and last_successful_pi_terminal is not None
                        and _is_pi_quiescent()
                    ):
                        pi_quiescence_deadline_monotonic = None
                        try:
                            await self._stop_connection_for_quiescence(receiver)
                        except Exception as exc:
                            if pi_subspawn_tracker.has_pending():
                                await self._terminate_pi_tracked_subspawns(
                                    spawn_id,
                                    pi_subspawn_tracker,
                                    reason="pi_quiescent_stop_failed",
                                )
                                pi_tracked_cleanup_reason = "pi_quiescent_stop_failed"
                            drain_error = RuntimeError(f"pi_quiescent_stop_failed:{exc}")
                            break
                        recorded_terminal_outcome = last_successful_pi_terminal
                    break
                except TimeoutError:
                    if not pi_quiescence_enabled or pi_quiescence_deadline_monotonic is None:
                        raise
                    pi_quiescence_deadline_monotonic = None
                    if last_successful_pi_terminal is None:
                        continue
                    try:
                        await self._stop_connection_for_quiescence(receiver)
                    except Exception as exc:
                        if pi_subspawn_tracker.has_pending():
                            await self._terminate_pi_tracked_subspawns(
                                spawn_id,
                                pi_subspawn_tracker,
                                reason="pi_quiescent_stop_failed",
                            )
                            pi_tracked_cleanup_reason = "pi_quiescent_stop_failed"
                        drain_error = RuntimeError(f"pi_quiescent_stop_failed:{exc}")
                        break
                    recorded_terminal_outcome = last_successful_pi_terminal
                    break

                pi_subspawn_tracker.observe(event)
                if is_pi_connection:
                    transition = activity_transition(event)
                    if transition == "turn_active":
                        pi_parent_idle = False
                    elif transition == "idle":
                        pi_parent_idle = True
                if tracer is not None:
                    tracer.emit(
                        "drain",
                        "event_received",
                        direction="inbound",
                        data={"event_type": event.event_type, "harness_id": event.harness_id},
                    )
                try:
                    write_result = self._history_writers[spawn_id].write(event)
                    if not write_result.success:
                        raise RuntimeError(write_result.error or "history write failed")
                    consecutive_write_failures = 0
                    if tracer is not None:
                        tracer.emit(
                            "drain",
                            "event_persisted",
                            data={"event_type": event.event_type},
                        )
                    self._observers.dispatch(spawn_id, event)
                except Exception as persist_exc:
                    consecutive_write_failures += 1
                    if tracer is not None:
                        tracer.emit(
                            "drain",
                            "persist_error",
                            data={
                                "event_type": event.event_type,
                                "error": str(persist_exc),
                                "consecutive_failures": consecutive_write_failures,
                            },
                        )
                    logger.warning(
                        "Failed to persist event for spawn %s (%d/%d consecutive failures)",
                        spawn_id,
                        consecutive_write_failures,
                        max_consecutive_failures,
                        exc_info=True,
                    )
                    if consecutive_write_failures >= max_consecutive_failures:
                        logger.error(
                            "Aborting drain loop for spawn %s after %d consecutive write failures",
                            spawn_id,
                            max_consecutive_failures,
                        )
                        drain_error = RuntimeError(
                            "Aborted drain loop after repeated output persistence failures"
                        )
                        self._fan_out_event(spawn_id, event)
                        break
                event_outcome = terminal_outcome(event)
                self._fan_out_event(spawn_id, event)
                if (
                    pi_quiescence_enabled
                    and pi_quiescence_deadline_monotonic is not None
                    and (_is_pi_lifecycle_event(event) or event_outcome is not None)
                ):
                    pi_quiescence_deadline_monotonic = None
                if (
                    is_pi_connection
                    and pi_subspawn_tracker.lifecycle_tracking_invalidated_error is not None
                ):
                    recorded_terminal_outcome = TerminalEventOutcome(
                        status="failed",
                        exit_code=1,
                        error=pi_subspawn_tracker.lifecycle_tracking_invalidated_error,
                    )
                    break
                if event_outcome is not None:
                    action: DrainAction = policy.classify(event_outcome)
                    if is_pi_connection and event_outcome.status == "succeeded":
                        last_successful_pi_terminal = event_outcome
                    if action.terminate:
                        if (
                            pi_quiescence_enabled
                            and event_outcome.status == "succeeded"
                        ):
                            # Defer success termination until the quiescence idle grace window
                            # elapses without new lifecycle/terminal events.
                            pass
                        elif (
                            is_pi_connection
                            and event_outcome.status == "succeeded"
                            and pi_subspawn_tracker.has_pending()
                        ):
                            pending_terminal_outcome = event_outcome
                            logger.info(
                                "Pi terminal event deferred until subspawn drain boundary "
                                "for spawn %s "
                                "(active_subspawns=%s anonymous_subspawns=%d)",
                                spawn_id,
                                sorted(pi_subspawn_tracker.active_ids),
                                pi_subspawn_tracker.anonymous_active_count,
                            )
                        else:
                            recorded_terminal_outcome = event_outcome
                            break
                    if action.emit_turn_boundary:
                        await self._fan_out_turn_boundary(spawn_id, event_outcome)
                if (
                    is_pi_connection
                    and pi_subspawn_tracker.notification_failure_error is not None
                    and pi_parent_idle
                    and not pi_subspawn_tracker.has_pending()
                ):
                    recorded_terminal_outcome = TerminalEventOutcome(
                        status="failed",
                        exit_code=1,
                        error=pi_subspawn_tracker.notification_failure_error,
                    )
                    break

                if pending_terminal_outcome is not None and not pi_subspawn_tracker.has_pending():
                    if pi_quiescence_enabled and pending_terminal_outcome.status == "succeeded":
                        last_successful_pi_terminal = pending_terminal_outcome
                        pending_terminal_outcome = None
                    else:
                        recorded_terminal_outcome = pending_terminal_outcome
                        break

                if pi_quiescence_enabled:
                    if last_successful_pi_terminal is not None and _is_pi_quiescent():
                        if pi_quiescence_deadline_monotonic is None:
                            pi_quiescence_deadline_monotonic = (
                                time.monotonic() + self._pi_quiescence_idle_grace_secs
                            )
                    else:
                        pi_quiescence_deadline_monotonic = None
        except asyncio.CancelledError:
            drain_cancelled = True
            raise
        except Exception as exc:
            drain_error = exc
            raise
        finally:
            if (
                is_pi_connection
                and pi_subspawn_tracker.has_pending()
                and pi_tracked_cleanup_reason is None
            ):
                await self._terminate_pi_tracked_subspawns(
                    spawn_id,
                    pi_subspawn_tracker,
                    reason="pi_process_exit_with_tracked_children",
                )
                pi_tracked_cleanup_reason = "pi_process_exit_with_tracked_children"
            self._observers.complete(spawn_id)
            self._fan_out_event(spawn_id, None)
            session = self._sessions.get(spawn_id)
            if session is not None:
                if drain_cancelled:
                    outcome = DrainOutcome(
                        status="cancelled",
                        exit_code=1,
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif drain_error is not None:
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error=str(drain_error),
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif session.cancel_sent:
                    outcome = DrainOutcome(
                        status="cancelled",
                        exit_code=143,
                        error="cancelled",
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif (
                    is_pi_connection
                    and recorded_terminal_outcome is None
                    and last_successful_pi_terminal is not None
                    and pi_subspawn_tracker.has_pending()
                ):
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error="pi_process_exited_with_tracked_children",
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif (
                    is_pi_connection
                    and pi_subspawn_tracker.notification_failure_error is not None
                    and recorded_terminal_outcome is None
                ):
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error=pi_subspawn_tracker.notification_failure_error,
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif recorded_terminal_outcome is not None:
                    outcome = DrainOutcome(
                        status=recorded_terminal_outcome.status,
                        exit_code=recorded_terminal_outcome.exit_code,
                        error=recorded_terminal_outcome.error,
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                else:
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error="connection_closed_without_terminal_event",
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                self._resolve_completion_future(session, outcome)
                cleanup_task = asyncio.create_task(self._cleanup_completed_session(spawn_id))
                self._cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(self._cleanup_tasks.discard)

    async def _stop_connection_for_quiescence(self, receiver: HarnessConnection[Any]) -> None:
        try:
            await cast("Callable[..., Awaitable[None]]", receiver.stop)(reason="quiescent")
        except TypeError:
            await receiver.stop()

    async def _terminate_pi_tracked_subspawns(
        self,
        spawn_id: SpawnId,
        tracker: _PiSubspawnTracker,
        *,
        reason: str,
    ) -> None:
        if os.name == "nt":
            return

        pgids = tracker.active_tracked_pgid_candidates()
        if not pgids:
            logger.warning(
                "Pi spawn %s ended with tracked children but no pid/pgid metadata for cleanup",
                spawn_id,
            )
            return

        for pgid in pgids:
            await self._terminate_posix_process_group(
                spawn_id=spawn_id,
                process_group_id=pgid,
                reason=reason,
            )

    async def _terminate_posix_process_group(
        self,
        *,
        spawn_id: SpawnId,
        process_group_id: int,
        reason: str,
    ) -> None:
        if os.name == "nt" or process_group_id <= 0:
            return

        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            logger.warning(
                "Failed SIGTERM cleanup for Pi child process group %d (spawn %s, reason=%s)",
                process_group_id,
                spawn_id,
                reason,
                exc_info=True,
            )
            return

        await asyncio.sleep(0.25)

        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            logger.warning(
                "Failed liveness check for Pi child process group %d (spawn %s, reason=%s)",
                process_group_id,
                spawn_id,
                reason,
                exc_info=True,
            )
            return

        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            logger.warning(
                "Failed SIGKILL cleanup for Pi child process group %d (spawn %s, reason=%s)",
                process_group_id,
                spawn_id,
                reason,
                exc_info=True,
            )

    def subscribe(self, spawn_id: SpawnId) -> asyncio.Queue[HarnessEvent | None] | None:
        """Attach one subscriber queue to the spawn, or return None if unavailable."""

        session = self._sessions.get(spawn_id)
        if session is None or session.subscriber is not None:
            return None
        session.subscriber = asyncio.Queue(maxsize=1000)
        return session.subscriber

    def unsubscribe(self, spawn_id: SpawnId) -> None:
        """Detach the current subscriber for one spawn."""

        session = self._sessions.get(spawn_id)
        if session is not None:
            session.subscriber = None

    async def wait_for_completion(self, spawn_id: SpawnId) -> DrainOutcome | None:
        """Await one spawn's terminal drain outcome, if still tracked."""

        completion_future = self._completion_futures.get(spawn_id)
        if completion_future is None:
            return None
        return await completion_future

    async def inject(
        self,
        spawn_id: SpawnId,
        message: str,
        source: str = "control_socket",
        on_result: InjectResultCallback | None = None,
    ) -> InjectResult:
        """Record and route one user message injection to the target connection."""

        record = spawn_store.get_spawn(self._runtime_root, spawn_id)
        if record is not None and record.status in TERMINAL_SPAWN_STATUSES:
            result = InjectResult(
                success=False,
                error=f"spawn not running: {record.status}",
            )
            if on_result is not None:
                on_result(result)
            return result

        session = self._sessions.get(spawn_id)
        if session is None:
            result = InjectResult(
                success=False,
                error=f"Spawn {spawn_id} is not active",
            )
            if on_result is not None:
                on_result(result)
            return result

        async def _send_message() -> int:
            inbound_seq = await self._record_inbound(
                spawn_id,
                action="user_message",
                data={"text": message},
                source=source,
            )
            await session.connection.send_user_message(message)
            return inbound_seq

        coordinator = session.control_actions
        if coordinator is None:
            try:
                inbound_seq = await _send_message()
            except Exception as exc:
                result = InjectResult(success=False, error=str(exc))
                if on_result is not None:
                    on_result(result)
                return result
            result = InjectResult(success=True, inbound_seq=inbound_seq)
            if on_result is not None:
                on_result(result)
            return result

        outcome = await coordinator.run_action(
            action=ControlActionType.INJECT,
            payload={"text": message},
            source=source,
            send=_send_message,
        )
        if not outcome.success:
            result = InjectResult(success=False, error=outcome.error or "inject_failed")
            if on_result is not None:
                on_result(result)
            return result

        inbound_seq = outcome.value
        result = InjectResult(
            success=True,
            inbound_seq=inbound_seq if isinstance(inbound_seq, int) else None,
        )
        if on_result is not None:
            on_result(result)
        return result

    async def interrupt(
        self,
        spawn_id: SpawnId,
        *,
        source: str = "runtime",
    ) -> None:
        """Send one serialized interrupt to an active spawn connection."""

        session = self._sessions.get(spawn_id)
        if session is None:
            raise RuntimeError(f"Spawn {spawn_id} is not active")

        coordinator = session.control_actions
        if coordinator is None:
            await session.connection.send_cancel()
            return

        outcome = await coordinator.run_action(
            action=ControlActionType.INTERRUPT,
            payload={},
            source=source,
            send=session.connection.send_cancel,
        )
        if not outcome.success:
            raise RuntimeError(outcome.error or "interrupt_failed")

    async def respond_request(
        self,
        spawn_id: SpawnId,
        *,
        request_id: str,
        decision: str,
        payload: dict[str, object] | None = None,
        source: str = "chat",
    ) -> None:
        """Send one serialized permission decision for a pending harness request."""

        session = self._sessions.get(spawn_id)
        if session is None:
            raise RuntimeError(f"Spawn {spawn_id} is not active")

        async def _respond() -> None:
            await session.connection.respond_request(request_id, decision, payload)

        coordinator = session.control_actions
        if coordinator is None:
            try:
                await _respond()
            except Exception as exc:
                await self._notify_runtime_request_failed(
                    session.connection,
                    request_id=request_id,
                    error=str(exc),
                )
                raise
            return

        response_payload: dict[str, object] = {
            "request_id": request_id,
            "decision": decision,
        }
        if payload is not None:
            response_payload["payload"] = payload

        outcome = await coordinator.run_action(
            action=ControlActionType.PERMISSION_REPLY,
            payload=response_payload,
            source=source,
            send=_respond,
        )
        if not outcome.success:
            error = outcome.error or "permission_reply_failed"
            if error != "stale_after_interrupt":
                await self._notify_runtime_request_failed(
                    session.connection,
                    request_id=request_id,
                    error=error,
                )
            raise RuntimeError(error)

    async def respond_user_input(
        self,
        spawn_id: SpawnId,
        *,
        request_id: str,
        answers: dict[str, object],
        source: str = "chat",
    ) -> None:
        """Send one serialized user-input response for a pending harness request."""

        session = self._sessions.get(spawn_id)
        if session is None:
            raise RuntimeError(f"Spawn {spawn_id} is not active")

        async def _respond() -> None:
            await session.connection.respond_user_input(request_id, answers)

        coordinator = session.control_actions
        if coordinator is None:
            try:
                await _respond()
            except Exception as exc:
                await self._notify_runtime_request_failed(
                    session.connection,
                    request_id=request_id,
                    error=str(exc),
                )
                raise
            return

        outcome = await coordinator.run_action(
            action=ControlActionType.USER_INPUT_REPLY,
            payload={"request_id": request_id, "answers": answers},
            source=source,
            send=_respond,
        )
        if not outcome.success:
            error = outcome.error or "user_input_reply_failed"
            if error != "stale_after_interrupt":
                await self._notify_runtime_request_failed(
                    session.connection,
                    request_id=request_id,
                    error=error,
                )
            raise RuntimeError(error)

    async def _notify_runtime_request_failed(
        self,
        connection: HarnessConnection[Any],
        *,
        request_id: str,
        error: str,
    ) -> None:
        callback = getattr(connection, "_notify_request_failed", None)
        if callback is None:
            return
        try:
            await cast("Callable[..., Awaitable[None]]", callback)(request_id, error=error)
        except Exception:
            logger.debug(
                "Runtime request failure callback raised for request %s",
                request_id,
                exc_info=True,
            )

    async def _record_inbound(
        self,
        spawn_id: SpawnId,
        action: str,
        data: dict[str, object],
        source: str,
    ) -> int:
        """Append one inbound action to the spawn write-ahead control log."""

        log_path = self._inbound_log_path(spawn_id)
        inbound_seq = await asyncio.to_thread(self._count_jsonl_lines, log_path)
        payload = {
            "action": action,
            "data": data,
            "ts": time.time(),
            "source": source,
        }
        await self._append_jsonl(log_path, payload)
        return inbound_seq

    def get_connection(self, spawn_id: SpawnId) -> HarnessConnection[Any] | None:
        """Return the active connection for one spawn, if present."""

        session = self._sessions.get(spawn_id)
        if session is None:
            return None
        return session.connection

    def get_tracer(self, spawn_id: SpawnId) -> DebugTracer | None:
        """Return the active debug tracer for one spawn, if present."""

        session = self._sessions.get(spawn_id)
        return session.debug_tracer if session is not None else None

    def get_history_seq(self, spawn_id: SpawnId) -> int:
        """Return the last-written history seq for one spawn, or -1 if none."""

        writer = self._history_writers.get(spawn_id)
        if writer is None:
            return -1
        return writer.last_seq

    async def stop_spawn(
        self,
        spawn_id: SpawnId,
        *,
        status: SpawnStatus = "cancelled",
        exit_code: int = 1,
        error: str | None = None,
        prefer_drain_outcome: bool = False,
    ) -> DrainOutcome | None:
        """Stop one managed spawn and clean up all associated resources."""

        session = self._sessions.get(spawn_id)
        if session is None:
            await self._stop_heartbeat(spawn_id)
            await self._observers.shutdown(spawn_id)
            return None

        if status == "cancelled" and not session.cancel_sent:
            session.cancel_sent = True
            with suppress(Exception):
                await self.interrupt(spawn_id, source="spawn_manager.stop_spawn")
            if not session.cancel_event_emitted:
                session.cancel_event_emitted = True
                await self._emit_cancelled_terminal_event(
                    spawn_id=spawn_id,
                    session=session,
                    exit_code=exit_code,
                    error=error,
                )

        fallback_outcome = DrainOutcome(
            status=status,
            exit_code=exit_code,
            error=error,
            duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
        )
        outcome = (
            self._resolve_completion_future(session, fallback_outcome)
            if not prefer_drain_outcome
            else fallback_outcome
        )

        if session.debug_tracer is not None:
            session.debug_tracer.close()

        # Capture scope state BEFORE connection.stop() — both connections clear
        # subprocess_pid and scope_snapshot inside stop(), so reading them after
        # would always yield None and make the safety pass a no-op.
        _pre_stop_pid = session.connection.subprocess_pid
        _pre_stop_scope = getattr(session.connection, "scope_snapshot", None)

        with suppress(Exception):
            await session.connection.stop()

        # Safety pass: if the process survived connection.stop() (e.g. the connection
        # was already dead before stop was called), force-terminate via the scope handle.
        with suppress(Exception):
            if _pre_stop_pid is not None and _pre_stop_scope is not None:
                try:
                    proc = psutil.Process(_pre_stop_pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    proc = None
                if proc is not None and proc.is_running():
                    logger.warning(
                        "Process %d still alive after connection.stop(); "
                        "scope safety cleanup for spawn %s",
                        _pre_stop_pid,
                        spawn_id,
                    )
                    from meridian.lib.platform.process_scope.fallback import terminate_tree_sync

                    await asyncio.to_thread(
                        terminate_tree_sync,
                        pid=_pre_stop_pid,
                        created_at_epoch=_pre_stop_scope.root_created_at_epoch,
                        grace_secs=5.0,
                        reason="stop_safety_pass",
                        scope_id=_pre_stop_scope.scope_id,
                    )

        # Give drain loop time to persist remaining events after connection closes.
        # The drain loop exits naturally once events() terminates, but enforce a
        # hard timeout to prevent indefinite blocking on a stuck drain.
        try:
            await asyncio.wait_for(asyncio.shield(session.drain_task), timeout=2.0)
        except (TimeoutError, asyncio.CancelledError, Exception):
            if prefer_drain_outcome:
                self._resolve_completion_future(session, fallback_outcome)
            session.drain_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await session.drain_task
        if session.drain_task.done() and not session.drain_task.cancelled():
            with suppress(Exception):
                session.drain_task.result()
        if prefer_drain_outcome:
            outcome = self._resolve_completion_future(session, fallback_outcome)
        await self._observers.shutdown(spawn_id)

        with suppress(Exception):
            await session.control_server.stop()

        await self._stop_heartbeat(spawn_id)
        self._fan_out_event(spawn_id, None)
        self._sessions.pop(spawn_id, None)
        self._completion_futures.pop(spawn_id, None)
        self._history_writers.pop(spawn_id, None)
        return outcome

    async def _emit_cancelled_terminal_event(
        self,
        *,
        spawn_id: SpawnId,
        session: SpawnSession,
        exit_code: int,
        error: str | None,
    ) -> None:
        terminal_event = HarnessEvent(
            event_type="cancelled",
            payload={
                "type": "cancelled",
                "status": "cancelled",
                "exit_code": exit_code,
                "error": error,
            },
            harness_id=session.connection.harness_id.value,
            raw_text=None,
        )
        history_writer = self._history_writers.get(spawn_id)
        if history_writer is not None:
            with suppress(Exception):
                history_writer.write(terminal_event)
                self._observers.dispatch(spawn_id, terminal_event)
        self._fan_out_event(spawn_id, terminal_event)

    async def _fan_out_turn_boundary(
        self,
        spawn_id: SpawnId,
        outcome: TerminalEventOutcome,
    ) -> None:
        """Emit a synthetic turn boundary event for persistent drain sessions."""

        synthetic = HarnessEvent(
            event_type=TURN_BOUNDARY_EVENT_TYPE,
            harness_id="meridian",
            payload={
                "status": outcome.status,
                "exit_code": outcome.exit_code,
                "synthetic": True,
            },
            raw_text=None,
        )
        history_writer = self._history_writers.get(spawn_id)
        if history_writer is not None:
            try:
                history_writer.write(synthetic)
                self._observers.dispatch(spawn_id, synthetic)
            except Exception as persist_exc:
                logger.warning(
                    "Failed to persist turn boundary event for spawn %s: %s",
                    spawn_id,
                    persist_exc,
                )
        self._fan_out_event(spawn_id, synthetic)

    async def shutdown(
        self,
        *,
        status: SpawnStatus = "cancelled",
        exit_code: int = 1,
        error: str | None = None,
    ) -> None:
        """Stop every active spawn and clear the session registry."""

        for spawn_id in list(self._sessions):
            await self.stop_spawn(
                spawn_id,
                status=status,
                exit_code=exit_code,
                error=error,
            )
        for spawn_id in list(self._heartbeat_tasks):
            await self._stop_heartbeat(spawn_id)
        for spawn_id in list(self._completion_futures):
            await self._observers.shutdown(spawn_id)
        self._completion_futures.clear()
        self._history_writers.clear()

    def list_spawns(self) -> list[SpawnId]:
        """List active spawn IDs."""

        return list(self._sessions)

    async def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        """Append one JSON line to a JSONL file (used for inbound control log)."""

        line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
        await asyncio.to_thread(append_text_line, path, line)

    def _count_jsonl_lines(self, path: Path) -> int:
        if not path.exists():
            return 0
        with path.open("rb") as handle:
            return sum(1 for _ in handle)

    def _fan_out_event(self, spawn_id: SpawnId, event: HarnessEvent | None) -> None:
        session = self._sessions.get(spawn_id)
        if session is None or session.subscriber is None:
            return
        if event is None:
            # Preserve the terminal sentinel even under backpressure.
            while True:
                try:
                    session.subscriber.put_nowait(None)
                    return
                except asyncio.QueueFull:
                    with suppress(asyncio.QueueEmpty):
                        session.subscriber.get_nowait()
                    continue
        try:
            session.subscriber.put_nowait(event)
            if session.debug_tracer is not None:
                session.debug_tracer.emit(
                    "drain",
                    "event_fanout",
                    data={"event_type": event.event_type},
                )
        except asyncio.QueueFull:
            from meridian.lib.telemetry import emit_telemetry
            from meridian.lib.telemetry.events import make_error_data

            error_data = make_error_data(message="Subscriber queue full, event dropped")
            error_data["error"]["type"] = "QueueFullBackpressure"

            emit_telemetry(
                "runtime",
                "runtime.stream_event_dropped",
                scope="streaming.spawn_manager",
                severity="warning",
                ids={"spawn_id": str(spawn_id)},
                data={**error_data, "event_type": event.event_type},
            )
            if session.debug_tracer is not None:
                session.debug_tracer.emit(
                    "drain",
                    "event_dropped",
                    data={"event_type": event.event_type, "reason": "queue_full"},
                )

    def _spawn_dir(self, spawn_id: SpawnId) -> Path:
        return self._runtime_root / "spawns" / str(spawn_id)

    def _history_path(self, spawn_id: SpawnId) -> Path:
        return self._spawn_dir(spawn_id) / "history.jsonl"

    def _inbound_log_path(self, spawn_id: SpawnId) -> Path:
        return self._spawn_dir(spawn_id) / "inbound.jsonl"

    async def _cleanup_completed_session(self, spawn_id: SpawnId) -> None:
        """Clean up resources after a receiver drain loop exits naturally."""

        await self._stop_heartbeat(spawn_id)
        session = self._sessions.pop(spawn_id, None)
        if session is None:
            await self._observers.shutdown(spawn_id)
            return
        if session.debug_tracer is not None:
            session.debug_tracer.close()
        if not session.drain_task.done():
            with suppress(asyncio.CancelledError, Exception):
                await session.drain_task
        if session.drain_task.done() and not session.drain_task.cancelled():
            with suppress(Exception):
                session.drain_task.result()
        with suppress(Exception):
            await session.connection.stop()
        with suppress(Exception):
            await session.control_server.stop()
        await self._observers.shutdown(spawn_id)
        self._history_writers.pop(spawn_id, None)

    def _resolve_completion_future(
        self,
        session: SpawnSession,
        outcome: DrainOutcome,
    ) -> DrainOutcome:
        if not session.completion_future.done():
            with suppress(asyncio.InvalidStateError):
                session.completion_future.set_result(outcome)
        if session.completion_future.done() and not session.completion_future.cancelled():
            return session.completion_future.result()
        return outcome


__all__ = ["DrainOutcome", "SpawnManager", "SpawnSession", "dispatch_start"]
