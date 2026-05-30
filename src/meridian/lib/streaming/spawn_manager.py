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
from meridian.lib.core.types import HarnessId, SpawnId, TransportId
from meridian.lib.harness import pi_lifecycle_events as pi_lifecycle
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
    DrainPolicy,
    SingleTurnDrainPolicy,
)
from meridian.lib.streaming.event_observers import (
    CallbackObserver,
    EventObserver,
    EventObserverRegistry,
    HarnessEventCallback,
)
from meridian.lib.streaming.heartbeat import heartbeat_loop
from meridian.lib.streaming.pi_drain import PiDrainCoordinator, PiSubspawnTracker
from meridian.lib.streaming.types import InjectResult

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


if TYPE_CHECKING:
    from meridian.lib.harness.connections.base import (
        ConnectionConfig,
        HarnessConnection,
    )
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.observability.debug_tracer import DebugTracer

logger = logging.getLogger(__name__)
InjectResultCallback = Callable[[InjectResult], None]


def _safe_connection_session_id(connection: object) -> str | None:
    """Read optional connection session_id without assuming full HarnessConnection shape."""

    try:
        session_id = cast("Any", connection).session_id
    except Exception:
        return None
    return session_id if isinstance(session_id, str) and session_id.strip() else None


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

    declared_transports = bundle.adapter.contract.transport.transport_ids
    transport_id = _select_dispatch_transport(declared_transports)
    connection_class = get_connection_class(config.harness_id, transport_id)
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


def _select_dispatch_transport(
    declared_transports: tuple[TransportId, ...],
) -> TransportId:
    """Choose dispatch transport from adapter contract declarations."""

    if len(declared_transports) == 1:
        return declared_transports[0]
    if TransportId.STREAMING in declared_transports:
        return TransportId.STREAMING
    return declared_transports[0]


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
        heartbeat_touch: Callable[[Path, SpawnId], None] | None = None,
        start_connection: StartConnectionPort | None = None,
        control_server_factory: ControlServerFactory | None = None,
    ):
        self._runtime_root = runtime_root
        self._project_root = project_root
        self._debug = debug
        self._heartbeat_interval_secs = heartbeat_interval_secs
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
            self._history_writers.pop(spawn_id, None)
            with suppress(Exception):
                await control_server.stop()
            with suppress(Exception):
                await connection.stop()
            raise

        drain_task = asyncio.create_task(
            self._drain_loop(
                spawn_id,
                connection,
                tracer,
                drain_policy=resolved_policy,
                pi_session_role=pi_session_role,
                notification_timeout_seconds=config.pi_notification_timeout_seconds,
                child_wave_timeout_seconds=config.pi_child_wave_timeout_seconds,
            )
        )
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
        notification_timeout_seconds: float | None = None,
        child_wave_timeout_seconds: float | None = None,
    ) -> None:
        """Durably append each harness event and fan out to the active subscriber.

        Writes a stable event envelope to history.jsonl so event type and harness
        identity are preserved even when the payload itself omits that metadata.
        """

        # Import at runtime to avoid circular import during module initialization.
        from meridian.lib.harness.semantics import activity_transition, terminal_outcome

        def _emit_pi_phase(*, phase: str, session_role: str | None, **payload: object) -> None:
            self._emit_pi_phase_event(
                spawn_id,
                receiver,
                phase=phase,
                session_role=session_role,
                **payload,
            )

        async def _terminate_tracked_pi_children(
            tracker: PiSubspawnTracker,
            reason: str,
        ) -> None:
            await self._terminate_pi_tracked_subspawns(spawn_id, tracker, reason=reason)

        consecutive_write_failures = 0
        max_consecutive_failures = 10
        drain_cancelled = False
        drain_error: Exception | None = None
        recorded_terminal_outcome: TerminalEventOutcome | None = None
        pi_drain = PiDrainCoordinator.for_connection(
            runtime_root=self._runtime_root,
            spawn_id=spawn_id,
            receiver=cast("HarnessConnection[ResolvedLaunchSpec]", receiver),
            session_role=pi_session_role,
            notification_timeout_seconds=notification_timeout_seconds,
            child_wave_timeout_seconds=child_wave_timeout_seconds,
            emit_phase=_emit_pi_phase,
        )
        await pi_drain.start()

        policy = drain_policy
        if policy is None:
            policy = (
                pi_drain.default_policy()
                if pi_drain.is_pi_connection
                else SingleTurnDrainPolicy()
            )
        pi_drain.set_policy(policy)

        events_iter = receiver.events().__aiter__()
        pending_event_task: asyncio.Future[HarnessEvent] | None = None
        try:
            while True:
                try:
                    next_timeout = pi_drain.next_timeout()
                    if pending_event_task is None:
                        pending_event_task = asyncio.ensure_future(anext(events_iter))
                    if next_timeout is not None:
                        event = await asyncio.wait_for(
                            asyncio.shield(pending_event_task),
                            timeout=next_timeout,
                        )
                    else:
                        event = await pending_event_task
                    pending_event_task = None
                except StopAsyncIteration:
                    pending_event_task = None
                    if pi_drain.quiescence_candidate is not None:
                        recorded_terminal_outcome = pi_drain.quiescence_candidate
                    break
                except TimeoutError:
                    timeout_outcome = await pi_drain.handle_timeout(
                        _terminate_tracked_pi_children,
                    )
                    if timeout_outcome is not None:
                        recorded_terminal_outcome = timeout_outcome
                        break
                    continue

                transition = activity_transition(event) if pi_drain.is_pi_connection else None
                duplicate_canonical_lifecycle_event = await pi_drain.observe_event(
                    event,
                    transition,
                )
                if duplicate_canonical_lifecycle_event:
                    continue

                if tracer is not None:
                    tracer.emit(
                        "drain",
                        "event_received",
                        direction="inbound",
                        data={"event_type": event.event_type, "harness_id": event.harness_id},
                    )
                history_writer = self._history_writers.get(spawn_id)
                if history_writer is not None:
                    try:
                        write_result = history_writer.write(event)
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
                                (
                                    "Aborting drain loop for spawn %s after %d "
                                    "consecutive write failures"
                                ),
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
                pi_drain.note_event_persisted(event)

                pi_lifecycle_error = pi_drain.lifecycle_error_outcome()
                if pi_lifecycle_error is not None:
                    recorded_terminal_outcome = pi_lifecycle_error
                    break

                if event_outcome is not None:
                    action = policy.classify(event_outcome)
                    pi_decision = pi_drain.handle_terminal_event(event, event_outcome, action)
                    if pi_decision.recorded_outcome is not None:
                        recorded_terminal_outcome = pi_decision.recorded_outcome
                        break
                    if pi_decision.emit_turn_boundary:
                        await self._fan_out_turn_boundary(spawn_id, event_outcome)

                pi_failure = pi_drain.failure_outcome_after_event()
                if pi_failure is not None:
                    recorded_terminal_outcome = pi_failure
                    break
                if recorded_terminal_outcome is None:
                    pi_drain.maybe_start_quiescence_after_event()
        except asyncio.CancelledError:
            drain_cancelled = True
            raise
        except Exception as exc:
            drain_error = exc
            raise
        finally:
            pending_pi_children_at_exit = pi_drain.pending_children_at_exit()
            if pending_event_task is not None and not pending_event_task.done():
                pending_event_task.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_event_task
            await pi_drain.stop()
            await pi_drain.cleanup_pending_children_at_exit(_terminate_tracked_pi_children)
            session = self._sessions.pop(spawn_id, None)
            if session is not None:
                fallback_pi_error = pi_drain.fallback_error_without_recorded_outcome()
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
                    pi_drain.is_pi_connection
                    and recorded_terminal_outcome is None
                    and pending_pi_children_at_exit
                ):
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error="pi_process_exited_with_tracked_children",
                        duration_secs=max(0.0, time.monotonic() - session.started_monotonic),
                    )
                elif fallback_pi_error is not None and recorded_terminal_outcome is None:
                    outcome = DrainOutcome(
                        status="failed",
                        exit_code=1,
                        error=fallback_pi_error,
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
                session_id = (
                    _safe_connection_session_id(receiver) if pi_drain.session_seen else None
                )
                pi_drain.emit_session_phase_if_needed(session_id)
                pi_drain.emit_finalized(
                    status=outcome.status,
                    exit_code=outcome.exit_code,
                    error=outcome.error,
                )
                self._resolve_completion_future(session, outcome)
                cleanup_task = asyncio.create_task(
                    self._cleanup_completed_session(spawn_id, session)
                )
                self._cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(self._cleanup_tasks.discard)
            self._observers.complete(spawn_id)
            if session is not None and session.subscriber is not None:
                while True:
                    try:
                        session.subscriber.put_nowait(None)
                        break
                    except asyncio.QueueFull:
                        with suppress(asyncio.QueueEmpty):
                            session.subscriber.get_nowait()
                        continue

    async def _terminate_pi_tracked_subspawns(
        self,
        spawn_id: SpawnId,
        tracker: PiSubspawnTracker,
        *,
        reason: str,
    ) -> None:
        pgids = tracker.active_tracked_pgid_candidates()
        if not pgids:
            logger.warning(
                "Pi spawn %s ended with tracked children but no pid/pgid metadata for cleanup",
                spawn_id,
            )
            return

        for pgid in pgids:
            if os.name == "nt":
                await self._terminate_process_tree_fallback(
                    spawn_id=spawn_id,
                    process_id=pgid,
                    reason=reason,
                )
            else:
                await self._terminate_posix_process_group(
                    spawn_id=spawn_id,
                    process_group_id=pgid,
                    reason=reason,
                )

    async def _terminate_process_tree_fallback(
        self,
        *,
        spawn_id: SpawnId,
        process_id: int,
        reason: str,
    ) -> None:
        if process_id <= 0:
            return

        try:
            from meridian.lib.platform.process_scope.fallback import terminate_tree_sync

            await asyncio.to_thread(
                terminate_tree_sync,
                pid=process_id,
                grace_secs=5.0,
                reason=reason,
                scope_id=f"pi-subspawn:{spawn_id}",
                degraded_fallback=True,
            )
        except Exception:
            logger.warning(
                "Failed fallback cleanup for Pi child process %d (spawn %s, reason=%s)",
                process_id,
                spawn_id,
                reason,
                exc_info=True,
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

    def _emit_pi_phase_event(
        self,
        spawn_id: SpawnId,
        connection: HarnessConnection[Any],
        *,
        phase: str,
        **data: object,
    ) -> None:
        payload: dict[str, object] = {
            "type": pi_lifecycle.PI_PHASE_EVENT_TYPE,
            "phase": phase,
            "spawn_id": str(spawn_id),
            "schema_version": pi_lifecycle.PI_SUPPORTED_LIFECYCLE_SCHEMA_VERSION,
        }
        for key, value in data.items():
            if value is not None:
                payload[key] = value

        event = HarnessEvent(
            event_type=pi_lifecycle.PI_PHASE_EVENT_TYPE,
            harness_id=connection.harness_id.value,
            payload=payload,
            raw_text=None,
        )
        history_writer = self._history_writers.get(spawn_id)
        if history_writer is not None:
            try:
                history_writer.write(event)
                self._observers.dispatch(spawn_id, event)
            except Exception as persist_exc:
                logger.warning(
                    "Failed to persist Pi phase event for spawn %s: %s",
                    spawn_id,
                    persist_exc,
                )
        self._fan_out_event(spawn_id, event)
        tracer = self.get_tracer(spawn_id)
        if tracer is not None:
            tracer.emit(
                "drain",
                "pi_phase",
                data=payload,
            )

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

    async def _cleanup_completed_session(
        self,
        spawn_id: SpawnId,
        session: SpawnSession,
    ) -> None:
        """Clean up resources after a receiver drain loop exits naturally."""

        await self._stop_heartbeat(spawn_id)
        if session.debug_tracer is not None:
            session.debug_tracer.close()
        if not session.drain_task.done():
            with suppress(asyncio.CancelledError, Exception):
                await session.drain_task
        if session.drain_task.done() and not session.drain_task.cancelled():
            with suppress(Exception):
                session.drain_task.result()
        if session.connection.harness_id is HarnessId.PI:
            self._emit_pi_phase_event(
                spawn_id,
                session.connection,
                phase="cleanup_running",
                cleanup_status="running",
            )
            try:
                stop_result = await session.connection.stop(reason="quiescent")
                cleanup_status = "escalated" if stop_result.escalated else "completed"
                if stop_result.escalated:
                    self._emit_pi_phase_event(
                        spawn_id,
                        session.connection,
                        phase="cleanup_escalated",
                        cleanup_status="escalated",
                        reason="abort_grace_expired",
                    )
                self._emit_pi_phase_event(
                    spawn_id,
                    session.connection,
                    phase="cleanup_completed",
                    cleanup_status=cleanup_status,
                )
            except Exception as exc:
                self._emit_pi_phase_event(
                    spawn_id,
                    session.connection,
                    phase="cleanup_failed",
                    cleanup_status="failed",
                    error=str(exc),
                )
        else:
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
