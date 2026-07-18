"""Runtime registry and durable drain for active harness connections."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import (
    RawHarnessEvent,
    reap_on_ownership_transfer_failure,
)
from meridian.lib.harness.control_action import (
    ControlActionCoordinator,
    ControlActionType,
)
from meridian.lib.launch.constants import LAST_OBSERVED_EVENT_FILENAME
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state import spawn_store
from meridian.lib.state.atomic import append_text_line
from meridian.lib.state.history import HarnessHistoryWriter
from meridian.lib.state.spawn_tree import terminate_recorded_spawn_scope
from meridian.lib.streaming.completion_contracts import CompletionCleanupRequest
from meridian.lib.streaming.control_socket import ControlSocketServer, control_socket_path
from meridian.lib.streaming.drain_coordinator import DrainPlan
from meridian.lib.streaming.drain_plan_factory import build_drain_plan
from meridian.lib.streaming.drain_policy import (
    TURN_BOUNDARY_EVENT_TYPE,
    DrainPolicy,
)
from meridian.lib.streaming.event_observers import (
    CallbackObserver,
    EventObserver,
    EventObserverRegistry,
    HarnessEventCallback,
)
from meridian.lib.streaming.heartbeat import heartbeat_loop
from meridian.lib.streaming.spawn_dispatch import dispatch_start
from meridian.lib.streaming.spawn_drain_loop import (
    SpawnDrainLoop,
    resolve_terminal_outcome,
)
from meridian.lib.streaming.spawn_session import DrainOutcome, SpawnSession
from meridian.lib.streaming.types import InjectResult

StartConnectionPort = Callable[
    ["ConnectionConfig", ResolvedLaunchSpec],
    Awaitable["HarnessConnection[Any]"],
]
ControlServerFactory = Callable[[SpawnId, Path, "SpawnManager"], ControlSocketServer]


if TYPE_CHECKING:
    from meridian.lib.core.spawn_service import SpawnApplicationService
    from meridian.lib.harness.connections.base import (
        ConnectionConfig,
        HarnessConnection,
    )
    from meridian.lib.harness.semantics import TerminalEventOutcome
    from meridian.lib.observability.debug_tracer import DebugTracer

logger = logging.getLogger(__name__)
InjectResultCallback = Callable[[InjectResult], None]


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


def _build_spawn_application_service(
    project_root: Path,
    runtime_root: Path,
) -> SpawnApplicationService:
    from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots

    return build_spawn_application_service_from_roots(project_root, runtime_root)


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
        self._cleanup_tasks: dict[SpawnId, asyncio.Task[None]] = {}
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
        socket_path = control_socket_path(self._runtime_root, spawn_id)

        tracer = config.debug_tracer
        if tracer is None and self._debug:
            from meridian.lib.observability.debug_tracer import DebugTracer as _DebugTracer

            tracer = _DebugTracer(
                spawn_id=str(spawn_id),
                debug_path=self._spawn_dir(spawn_id) / "debug.jsonl",
            )

        connection: HarnessConnection[Any] | None = None
        control_server: ControlSocketServer | None = None
        drain_task: asyncio.Task[None] | None = None

        async def _cleanup_unregistered_start() -> object:
            if tracer is not None:
                tracer.close()
            await self._observers.shutdown(spawn_id)
            self._history_writers.pop(spawn_id, None)
            if drain_task is not None and not drain_task.done():
                drain_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await drain_task
            if control_server is not None:
                with suppress(BaseException):
                    await control_server.stop()
            if connection is not None:
                with suppress(BaseException):
                    await connection.stop()
            return None

        try:
            connection = await self._start_connection(config, spec)
            resolved_policy = drain_policy
            self._history_writers[spawn_id] = HarnessHistoryWriter(
                self._history_path(spawn_id),
                last_observed_event_path=(
                    self._spawn_dir(spawn_id) / LAST_OBSERVED_EVENT_FILENAME
                ),
            )
            if on_event is not None:
                self.register_observer(spawn_id, CallbackObserver(on_event))
            control_server = self._control_server_factory(
                spawn_id,
                socket_path,
                self,
            )

            await control_server.start()

            drain_plan = self._select_drain_plan(
                spawn_id=spawn_id,
                receiver=connection,
                config=config,
            )
            if resolved_policy is not None:
                drain_plan = drain_plan.with_policy(resolved_policy)
            drain_task = asyncio.create_task(
                self._drain_loop(
                    spawn_id,
                    connection,
                    drain_plan,
                    tracer,
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
                raw_terminal_frames_authoritative=drain_plan.raw_terminal_frames_authoritative,
                teardown=drain_plan.teardown,
                drain_plan=drain_plan,
                control_actions=ControlActionCoordinator(
                    spawn_id=spawn_id,
                    spawn_dir=self._spawn_dir(spawn_id),
                ),
            )
        except BaseException:
            await reap_on_ownership_transfer_failure(_cleanup_unregistered_start)
            raise

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
        if session is None or session.terminal_published:
            return None
        return session.control_server.endpoint

    async def _drain_loop(
        self,
        spawn_id: SpawnId,
        receiver: HarnessConnection[Any],
        drain_plan: DrainPlan,
        tracer: DebugTracer | None = None,
    ) -> None:
        drain_loop = SpawnDrainLoop(
            sessions=self._sessions,
            history_writers=self._history_writers,
            observers=self._observers,
            publish_terminal=self._publish_terminal,
            fan_out_event=self._fan_out_event,
            fan_out_turn_boundary=self._fan_out_turn_boundary,
        )
        await drain_loop.run(
            spawn_id=spawn_id,
            receiver=receiver,
            drain_plan=drain_plan,
            tracer=tracer,
        )

    def _select_drain_plan(
        self,
        *,
        spawn_id: SpawnId,
        receiver: HarnessConnection[Any],
        config: ConnectionConfig,
    ) -> DrainPlan:
        """Choose drain-loop configuration before entering the loop."""
        return build_drain_plan(
            project_root=self._project_root,
            runtime_root=self._runtime_root,
            spawn_id=spawn_id,
            receiver=receiver,
            config=config,
            emit_event=self.emit_event,
            inject=self.inject,
            build_spawn_application_service=_build_spawn_application_service,
        )

    def raw_terminal_frames_are_authoritative(self, spawn_id: SpawnId) -> bool:
        """Return whether raw harness terminal frames may finalize this spawn.

        Coordinators own harness-specific terminal policy.  The runner uses this
        observer seam to avoid treating a raw turn boundary as final when the
        selected coordinator must first wait for descendant work or a deadline.
        """

        session = self._sessions.get(spawn_id)
        if session is None:
            return True
        return session.raw_terminal_frames_authoritative

    def subscribe(self, spawn_id: SpawnId) -> asyncio.Queue[RawHarnessEvent | None] | None:
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
        if session is None or session.terminal_published:
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

        outcome = await session.control_actions.run_action(
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

        outcome = await session.control_actions.run_action(
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

        response_payload: dict[str, object] = {
            "request_id": request_id,
            "decision": decision,
        }
        if payload is not None:
            response_payload["payload"] = payload

        outcome = await session.control_actions.run_action(
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

        outcome = await session.control_actions.run_action(
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
        if session is None or session.terminal_published:
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
        status: SpawnStatus = SpawnStatus.CANCELLED,
        exit_code: int = 1,
        error: str | None = None,
    ) -> DrainOutcome | None:
        """Stop one managed spawn and clean up all associated resources."""

        session = self._sessions.get(spawn_id)
        if session is None:
            await self._stop_heartbeat(spawn_id)
            await self._observers.shutdown(spawn_id)
            return None
        if session.terminal_published:
            cleanup_task = self._cleanup_tasks.get(spawn_id)
            if cleanup_task is not None:
                await asyncio.gather(cleanup_task, return_exceptions=True)
            if session.completion_future.done() and not session.completion_future.cancelled():
                return session.completion_future.result()
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
        session.authoritative_stop_outcome = fallback_outcome
        outcome = fallback_outcome

        if session.debug_tracer is not None:
            session.debug_tracer.close()

        with suppress(Exception):
            await session.connection.stop(reason="stop_spawn")

        self._terminate_recorded_spawn_scope_backstop(spawn_id)

        # Give drain loop time to persist remaining events after connection closes.
        # The drain loop exits naturally once events() terminates, but enforce a
        # hard timeout to prevent indefinite blocking on a stuck drain.
        try:
            await asyncio.wait_for(asyncio.shield(session.drain_task), timeout=2.0)
        except (TimeoutError, asyncio.CancelledError, Exception):
            session.drain_task.cancel()
            # Terminal publication is owned by stop_spawn(), not by the
            # cancellable tail of the drain task.
            outcome = self._publish_terminal(
                spawn_id,
                session,
                fallback_outcome,
                None,
            )
            with suppress(asyncio.CancelledError, Exception):
                await session.drain_task
        if session.drain_task.done() and not session.drain_task.cancelled():
            with suppress(Exception):
                session.drain_task.result()
        outcome = self._publish_terminal(
            spawn_id,
            session,
            fallback_outcome,
            None,
        )
        cleanup_task = self._cleanup_tasks.get(spawn_id)
        if cleanup_task is not None:
            await asyncio.gather(cleanup_task, return_exceptions=True)
        await self._observers.shutdown(spawn_id)

        with suppress(Exception):
            await session.control_server.stop()

        await self._stop_heartbeat(spawn_id)
        self._fan_out_event(spawn_id, None)
        self._sessions.pop(spawn_id, None)
        self._completion_futures.pop(spawn_id, None)
        self._history_writers.pop(spawn_id, None)
        return outcome

    def _publish_terminal(
        self,
        spawn_id: SpawnId,
        session: SpawnSession,
        drain_outcome: DrainOutcome,
        cleanup_request: CompletionCleanupRequest | None,
    ) -> DrainOutcome:
        """Publish one terminal lifecycle exactly once, independent of drain cancellation."""

        if session.terminal_published:
            return self._resolve_completion_future(session, drain_outcome)

        outcome = resolve_terminal_outcome(
            drain_outcome,
            session.authoritative_stop_outcome,
        )
        session.terminal_published = True
        finalizer = session.drain_plan.finalizer
        if finalizer is not None:
            try:
                finalizer.after_finalized(
                    connection_session_id=_safe_connection_session_id(session.connection),
                    outcome=outcome,
                )
            except Exception:
                logger.exception("Terminal finalizer failed for spawn %s", spawn_id)
        self._resolve_completion_future(session, outcome)
        self._observers.complete(spawn_id)
        self._fan_out_event(spawn_id, None)
        cleanup_task = asyncio.create_task(
            self._run_post_publication_teardown(
                cleanup_request=cleanup_request,
                spawn_id=spawn_id,
                session=session,
                outcome=outcome,
            )
        )
        session.cleanup_task = cleanup_task
        self._cleanup_tasks[spawn_id] = cleanup_task
        cleanup_task.add_done_callback(
            lambda done, sid=spawn_id: self._drop_cleanup_task(sid, done)
        )
        return outcome

    def _drop_cleanup_task(
        self,
        spawn_id: SpawnId,
        task: asyncio.Task[None],
    ) -> None:
        if self._cleanup_tasks.get(spawn_id) is task:
            self._cleanup_tasks.pop(spawn_id, None)

    async def _run_post_publication_teardown(
        self,
        *,
        cleanup_request: CompletionCleanupRequest | None,
        spawn_id: SpawnId,
        session: SpawnSession,
        outcome: DrainOutcome,
    ) -> None:
        try:
            coordinator = session.drain_plan.coordinator
            if coordinator is not None and cleanup_request is not None:
                await coordinator.execute_post_publication_cleanup(cleanup_request)
        finally:
            await self._cleanup_completed_session(spawn_id, session, outcome)

    def _terminate_recorded_spawn_scope_backstop(self, spawn_id: SpawnId) -> None:
        """Best-effort recorded scope reap after transport stop attempts."""

        try:
            record = spawn_store.get_spawn(self._runtime_root, spawn_id)
            if record is None:
                return
            terminate_recorded_spawn_scope(
                self._runtime_root,
                record,
                reason="stop_spawn",
            )
        except Exception as exc:
            logger.warning(
                "Failed to run stop_spawn scope cleanup backstop for spawn %s: %s",
                spawn_id,
                exc,
            )

    async def _emit_cancelled_terminal_event(
        self,
        *,
        spawn_id: SpawnId,
        session: SpawnSession,
        exit_code: int,
        error: str | None,
    ) -> None:
        terminal_event = RawHarnessEvent(
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

        synthetic = RawHarnessEvent(
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

    def emit_event(self, spawn_id: SpawnId, event: RawHarnessEvent) -> None:
        """Persist and publish one manager-authored harness event."""

        history_writer = self._history_writers.get(spawn_id)
        if history_writer is not None:
            try:
                history_writer.write(event)
                self._observers.dispatch(spawn_id, event)
            except Exception as persist_exc:
                logger.warning(
                    "Failed to persist event %s for spawn %s: %s",
                    event.event_type,
                    spawn_id,
                    persist_exc,
                )
        self._fan_out_event(spawn_id, event)
        tracer = self.get_tracer(spawn_id)
        if tracer is not None:
            trace_event = f"{event.harness_id}_{event.event_type.rsplit('.', 1)[-1]}"
            tracer.emit(
                "drain",
                trace_event,
                data=event.payload,
            )

    async def shutdown(
        self,
        *,
        status: SpawnStatus = SpawnStatus.CANCELLED,
        exit_code: int = 1,
        error: str | None = None,
    ) -> None:
        """Stop every active spawn and clear the session registry."""

        active_spawn_ids = [
            spawn_id
            for spawn_id, session in self._sessions.items()
            if not session.terminal_published
        ]
        for spawn_id in active_spawn_ids:
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
        if self._cleanup_tasks:
            await asyncio.gather(
                *tuple(self._cleanup_tasks.values()),
                return_exceptions=True,
            )
        self._completion_futures.clear()
        self._history_writers.clear()

    def list_spawns(self) -> list[SpawnId]:
        """List active spawn IDs."""

        return [
            spawn_id
            for spawn_id, session in self._sessions.items()
            if not session.terminal_published
        ]

    async def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        """Append one JSON line to a JSONL file (used for inbound control log)."""

        line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
        await asyncio.to_thread(append_text_line, path, line)

    def _count_jsonl_lines(self, path: Path) -> int:
        if not path.exists():
            return 0
        with path.open("rb") as handle:
            return sum(1 for _ in handle)

    def _fan_out_event(self, spawn_id: SpawnId, event: RawHarnessEvent | None) -> None:
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
        outcome: DrainOutcome,
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
        with suppress(Exception):
            await session.teardown.stop_connection(session.connection, outcome)
        with suppress(Exception):
            await session.control_server.stop()
        await self._observers.shutdown(spawn_id)
        self._history_writers.pop(spawn_id, None)
        if self._sessions.get(spawn_id) is session:
            self._sessions.pop(spawn_id, None)

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


def _safe_connection_session_id(connection: object) -> str | None:
    """Read optional connection session id without assuming a concrete adapter."""

    try:
        session_id = cast("Any", connection).session_id
    except Exception:
        return None
    return session_id if isinstance(session_id, str) and session_id.strip() else None


__all__ = ["DrainOutcome", "SpawnManager", "SpawnSession", "dispatch_start"]
