"""Primary attach launcher for managed-backend primary sessions.

Orchestrates: backend connection (owner: connection class) + TUI subprocess + metadata sidecar.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock, RLock, Thread
from types import MappingProxyType
from typing import Any

import psutil
import structlog

from meridian.lib.core.event_hooks import run_event_hooks
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.connections.base import (
    ConnectionConfig,
    HarnessConnection,
    RawHarnessEvent,
)
from meridian.lib.harness.connections.errors import PortBindError
from meridian.lib.harness.extractors.base import AttemptFold
from meridian.lib.harness.idle_types import IdleSensorContext
from meridian.lib.harness.registry import get_harness_bundle
from meridian.lib.harness.semantics import normalize_event
from meridian.lib.idle import sidecar as idle_sidecar
from meridian.lib.launch.constants import LAUNCHER_LOG_FILENAME
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.signals import SignalCallbackReceiver, signal_coordinator
from meridian.lib.platform.launch_gate import ProcessIdentity
from meridian.lib.platform.process_scope import scope_from_identity, terminate_scope_sync
from meridian.lib.platform.process_scope.base import (
    ProcessScopeSnapshot,
)
from meridian.lib.state.primary_meta import ActivityState, PrimaryMetadata, write_primary_metadata
from meridian.lib.state.process_scope_projection import record_scope
from meridian.lib.streaming.heartbeat import heartbeat_loop

from .ports import TUI_TERMINATE_GRACE_SECONDS, LaunchedProcess, ProcessLauncher, RunningProcess
from .terminal_ownership import tui_terminal_window

TuiCommandBuilder = Callable[[str], tuple[str, ...]]
MAX_PORT_RETRY_ATTEMPTS = 3
_TUI_LAUNCH_DRAIN_TIMEOUT_SECONDS = 1.0
_TUI_WAIT_CANCEL_TIMEOUT_SECONDS = 1.0

logger = structlog.get_logger(__name__)


class _TuiExitOrdering:
    """Record whether launcher cancellation or the TUI exit was observed first."""

    def __init__(self) -> None:
        # Signal handlers can interrupt main-thread reads while this is held;
        # reentrancy keeps record_signal() from deadlocking the same thread.
        self._lock = RLock()
        self._signal_received = False
        self._exit_observed = False
        self._signal_preceded_exit = False

    def record_signal(self) -> None:
        with self._lock:
            self._signal_received = True
            if not self._exit_observed:
                self._signal_preceded_exit = True

    def record_exit(self) -> None:
        with self._lock:
            self._exit_observed = True

    @property
    def signal_received(self) -> bool:
        with self._lock:
            return self._signal_received

    @property
    def signal_preceded_exit(self) -> bool:
        with self._lock:
            return self._signal_preceded_exit


def _start_process_wait(
    running_process: RunningProcess,
    *,
    on_exit_observed: Callable[[], None] | None = None,
) -> asyncio.Future[LaunchedProcess]:
    """Run a synchronous process wait without joining it during loop shutdown."""

    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[LaunchedProcess] = loop.create_future()

    def _publish_result(result: LaunchedProcess) -> None:
        if not result_future.done():
            result_future.set_result(result)

    def _publish_error(error: BaseException) -> None:
        if not result_future.done():
            result_future.set_exception(error)

    def _wait() -> None:
        try:
            result = running_process.wait()
        except BaseException as exc:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_publish_error, exc)
            return
        if on_exit_observed is not None:
            on_exit_observed()
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(_publish_result, result)

    Thread(
        target=_wait,
        name=f"meridian-primary-wait-{running_process.identity.pid}",
        daemon=True,
    ).start()
    return result_future


class PrimaryAttachError(Exception):
    """Managed backend startup failed; caller should fall back to black-box path."""


def _process_birth_epoch(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None


@dataclass
class _LauncherMetadata:
    """Mutable working copy for launcher writes."""

    managed_backend: bool
    launcher_pid: int = field(default_factory=os.getpid)
    launcher_birth_epoch: float | None = field(
        default_factory=lambda: _process_birth_epoch(os.getpid())
    )
    backend_pid: int | None = None
    backend_birth_epoch: float | None = None
    tui_pid: int | None = None
    tui_birth_epoch: float | None = None
    backend_port: int | None = None
    activity: ActivityState = "starting"
    harness_session_id: str | None = None

    def to_primary_metadata(self) -> PrimaryMetadata:
        return PrimaryMetadata(
            managed_backend=self.managed_backend,
            launcher_pid=self.launcher_pid,
            launcher_birth_epoch=self.launcher_birth_epoch,
            backend_pid=self.backend_pid,
            backend_birth_epoch=self.backend_birth_epoch,
            tui_pid=self.tui_pid,
            tui_birth_epoch=self.tui_birth_epoch,
            backend_port=self.backend_port,
            activity=self.activity,
            harness_session_id=self.harness_session_id,
        )


@dataclass(frozen=True)
class PrimaryAttachOutcome:
    """Result of a primary attach launch."""

    exit_code: int
    session_id: str | None
    tui_pid: int | None
    cancelled: bool = False
    facts: AttemptFacts = field(default_factory=AttemptFacts)
    launcher_diagnostics_path: Path | None = None


class _StartupTelemetry:
    """Single-line startup progress output for managed primary attach."""

    def __init__(self) -> None:
        self._enabled = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        self._last_message = ""

    def update(self, message: str) -> None:
        if not self._enabled:
            return
        padding = " " * max(0, len(self._last_message) - len(message))
        sys.stderr.write(f"\r{message}{padding}")
        sys.stderr.flush()
        self._last_message = message

    def clear(self) -> None:
        if not self._enabled or not self._last_message:
            return
        sys.stderr.write(f"\r{' ' * len(self._last_message)}\r")
        sys.stderr.flush()
        self._last_message = ""

    def fail(self) -> None:
        if not self._enabled or not self._last_message:
            return
        sys.stderr.write(f"\r{self._last_message} failed\n")
        sys.stderr.flush()
        self._last_message = ""


def _startup_phase_message(connection: HarnessConnection[Any]) -> str:
    harness_label = connection.harness_id.value.capitalize()
    if connection.harness_id is HarnessId.CODEX:
        return f"Starting {harness_label} app-server..."
    return f"Starting {harness_label} managed session..."


class PrimaryAttachLauncher:
    """Manages lifecycle: connection (owns backend) + TUI process + metadata."""

    def __init__(
        self,
        *,
        spawn_id: SpawnId,
        spawn_dir: Path,
        connection: HarnessConnection[Any],
        tui_command_builder: TuiCommandBuilder,
        process_launcher: ProcessLauncher,
        runtime_root: Path | None = None,
        on_running: Callable[[ProcessIdentity], None] | None = None,
        fold: AttemptFold | None = None,
        session_id_observer: Callable[[str], None] | None = None,
    ) -> None:
        self._spawn_id = spawn_id
        self._spawn_dir = spawn_dir
        self._connection = connection
        self._tui_command_builder = tui_command_builder
        self._process_launcher = process_launcher
        self._runtime_root = runtime_root
        self._on_running = on_running
        self._harness_bundle = get_harness_bundle(connection.harness_id)
        self._fold = fold or self._harness_bundle.extractor.create_fold()
        self._event_hooks: tuple[Callable[[RawHarnessEvent], None], ...] = (self._fold,)
        if runtime_root is not None:
            self._event_hooks += self._harness_bundle.event_sinks(runtime_root, spawn_id)
        self._facts = self._fold.facts
        self._session_id_observer = session_id_observer
        self._metadata = _LauncherMetadata(managed_backend=True)
        self._metadata_lock = Lock()
        self._event_consumer_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._idle_error_reporter: idle_sidecar.SensorErrorReporter | None = None
        self._tui_scope_snapshot: ProcessScopeSnapshot | None = None
        self._running_process: RunningProcess | None = None
        self._tui_exit_ordering = _TuiExitOrdering()

    def _request_signal_cancel(self) -> None:
        """Handle SIGTERM/SIGHUP: stop the TUI wait so ``run`` can finalize."""

        self._tui_exit_ordering.record_signal()
        running = self._running_process
        if running is not None:
            running.cancel_wait()

    def _start_idle_sensor(
        self,
        *,
        session_id: str,
        env: dict[str, str],
        launch_task: asyncio.Future[LaunchedProcess],
    ) -> None:
        sensor_factory = self._harness_bundle.primary_idle_sensor
        if sensor_factory is None:
            return

        ctx = IdleSensorContext(
            connection=self._connection,
            harness_id=self._connection.harness_id,
            harness_session_id=session_id,
            env=MappingProxyType(dict(env)),
            tmux_pane=env.get("TMUX_PANE"),
            tui_alive=lambda: not launch_task.done(),
            spawn_dir=self._spawn_dir,
            spawn_id=self._spawn_id,
        )
        error_reporter = idle_sidecar.SensorErrorReporter(ctx)
        try:
            sensor = sensor_factory(ctx)
        except BaseException as exc:
            error_reporter.record(phase="create", error=exc)
            error_reporter.close()
            return
        if sensor is None:
            error_reporter.close()
            return
        self._idle_error_reporter = error_reporter

        def _on_raw_event(event: RawHarnessEvent) -> None:
            try:
                sensor.on_raw_event(event)
            except BaseException as exc:
                error_reporter.record(phase="raw_event", error=exc)

        self._event_hooks += (_on_raw_event,)
        self._idle_task = asyncio.create_task(
            idle_sidecar.run(
                sensor,
                ctx,
                error_reporter=error_reporter,
                autocompact_off=self._harness_bundle.autocompact_off,
                detect_ttl=self._harness_bundle.detect_ttl,
                compact_costs=self._harness_bundle.compact_costs,
            )
        )

    async def _stop_idle_task(self) -> None:
        """Stop the sensor before tearing down resources it may still use."""

        idle_task = self._idle_task
        error_reporter = self._idle_error_reporter
        self._idle_task = None
        self._idle_error_reporter = None
        if idle_task is None:
            if error_reporter is not None:
                error_reporter.close()
            return
        idle_task.cancel()
        try:
            with suppress(asyncio.CancelledError, Exception):
                await idle_task
        finally:
            if error_reporter is not None:
                error_reporter.close()

    async def run(
        self,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
        cwd: Path,
        env: dict[str, str],
        on_running: Callable[[ProcessIdentity], None] | None = None,
    ) -> PrimaryAttachOutcome:
        """Execute the full primary attach lifecycle."""

        if self._runtime_root is None:
            self._spawn_dir.mkdir(parents=True, exist_ok=True)
        if self._runtime_root is not None:
            self._heartbeat_task = asyncio.create_task(
                heartbeat_loop(self._runtime_root, self._spawn_id)
            )
        self._tui_exit_ordering = _TuiExitOrdering()
        cancel_receiver = SignalCallbackReceiver(
            target_signals=(signal.SIGTERM, signal.SIGHUP),
            callback=lambda _signum: self._request_signal_cancel(),
            escalate_on_repeat=True,
        )
        sigint_receiver = SignalCallbackReceiver(
            target_signals=(signal.SIGINT,),
            callback=lambda _signum: None,
        )
        signal_coordinator().register_receiver(cancel_receiver)
        sigint_receiver_registered = False
        connection_started = False
        session_id: str | None = None
        running_process: RunningProcess | None = None
        launch_task: asyncio.Future[LaunchedProcess] | None = None
        tui_lifecycle_finished = False
        telemetry = _StartupTelemetry()

        try:
            telemetry.update(_startup_phase_message(self._connection))

            config = await self._start_primary_observer_connection_with_retry(
                config=config,
                spec=spec,
            )
            connection_started = True
            session_id = self._connection.session_id

            with self._metadata_lock:
                self._metadata.backend_pid = self._connection.subprocess_pid
                self._metadata.backend_birth_epoch = (
                    _process_birth_epoch(self._connection.subprocess_pid)
                    if self._connection.subprocess_pid is not None
                    else None
                )
                self._metadata.backend_port = self._resolve_backend_port()
            self._write_metadata()

            self._fold.bind_scope(session_id)
            self._set_harness_session_id(session_id)
            self._record_backend_scope_from_connection(session_id)
            self._event_consumer_task = asyncio.create_task(self._consume_live_events())
            self._set_activity("idle")

            if session_id is None or not session_id.strip():
                raise RuntimeError(
                    f"Managed primary attach requires a harness session id "
                    f"(spawn_id={self._spawn_id})"
                )

            telemetry.update(f"Attaching {self._connection.harness_id.value.capitalize()} TUI...")
            command = tuple(self._tui_command_builder(session_id))
            running_callback = on_running if on_running is not None else self._on_running

            def _handle_running(identity: ProcessIdentity) -> None:
                self._set_tui_pid(identity.pid)
                if running_callback is not None:
                    running_callback(identity)
                self._record_tui_scope(identity, session_id)

            telemetry.clear()
            signal_coordinator().register_receiver(sigint_receiver)
            sigint_receiver_registered = True
            with tui_terminal_window(self._spawn_dir / LAUNCHER_LOG_FILENAME) as window:
                try:
                    running_process = self._process_launcher.start(
                        command=command,
                        cwd=cwd,
                        env=self._tui_env(env),
                        output_log_path=None,
                        terminal_fds=window.terminal_fds,
                    )
                    self._running_process = running_process
                    if self._tui_exit_ordering.signal_received:
                        # A signal arrived during backend startup; honor it now that the
                        # TUI process exists.
                        running_process.cancel_wait()
                    try:
                        _handle_running(running_process.identity)
                    except BaseException:
                        running_process.terminate()
                        await _start_process_wait(running_process)
                        raise
                    launch_task = _start_process_wait(
                        running_process, on_exit_observed=self._tui_exit_ordering.record_exit
                    )
                    self._start_idle_sensor(
                        session_id=session_id,
                        env=env,
                        launch_task=launch_task,
                    )
                    # The TUI owns the primary. Observer silence is not a quit.
                    launched = await asyncio.shield(launch_task)
                    # A pane close can make the TUI return zero. Observation order,
                    # not its exit code, distinguishes cancellation from normal quit.
                    cancelled = self._tui_exit_ordering.signal_preceded_exit
                    if cancelled:
                        # The wait stopped on the signal; ensure the TUI scope is gone.
                        await self._stop_idle_task()
                        await self._stop_tui_launch_task(
                            running_process,
                            launch_task,
                            reason="signal_cancelled",
                        )
                    tui_lifecycle_finished = True
                finally:
                    if (
                        running_process is not None
                        and launch_task is not None
                        and not tui_lifecycle_finished
                    ):
                        await self._stop_idle_task()
                        await self._stop_tui_launch_task(
                            running_process,
                            launch_task,
                            reason="primary_attach_shutdown",
                        )

            return PrimaryAttachOutcome(
                facts=self._facts,
                exit_code=130 if cancelled else launched.exit_code,
                session_id=session_id,
                tui_pid=launched.pid,
                cancelled=cancelled,
                launcher_diagnostics_path=window.captured_path,
            )
        except Exception:
            telemetry.fail()
            if self._tui_exit_ordering.signal_received:
                # A signal arrived before the TUI was interruptible (e.g. during
                # backend startup) and startup failed. Honor the cancellation
                # instead of surfacing a failure the fallback path would relaunch.
                logger.info(
                    "primary_attach.signal_cancel_during_startup",
                    spawn_id=str(self._spawn_id),
                )
                return PrimaryAttachOutcome(
                    facts=self._facts,
                    exit_code=130,
                    session_id=session_id,
                    tui_pid=None,
                    cancelled=True,
                )
            raise
        finally:
            if sigint_receiver_registered:
                signal_coordinator().unregister_receiver(sigint_receiver)
            signal_coordinator().unregister_receiver(cancel_receiver)
            self._running_process = None
            if connection_started:
                self._set_activity("finalizing")
            consumer_task = self._event_consumer_task
            if consumer_task is not None:
                consumer_task.cancel()
                try:
                    await consumer_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # An observer failure must not mask cancellation or skip
                    # TUI/backend teardown.
                    logger.warning(
                        "primary_attach.observer_failed",
                        spawn_id=str(self._spawn_id),
                        exc_info=True,
                    )
                self._event_consumer_task = None
            await self._stop_idle_task()
            heartbeat_task = self._heartbeat_task
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
                self._heartbeat_task = None
            if connection_started:
                await self._connection.stop()

    async def _start_primary_observer_connection_with_retry(
        self,
        *,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> ConnectionConfig:
        current_config = config
        for attempt in range(MAX_PORT_RETRY_ATTEMPTS):
            try:
                await self._start_primary_observer_connection(config=current_config, spec=spec)
                return current_config
            except PortBindError as exc:
                if attempt + 1 >= MAX_PORT_RETRY_ATTEMPTS:
                    raise PrimaryAttachError(
                        "Port bind failed after "
                        f"{MAX_PORT_RETRY_ATTEMPTS} attempts; managed primary startup failed"
                    ) from exc
                current_config = self._with_fresh_retry_port(current_config)
        raise PrimaryAttachError("Managed primary attach startup did not converge")

    async def _start_primary_observer_connection(
        self,
        *,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
    ) -> None:
        await self._connection.start_observer(config, spec)

    def _write_metadata(self) -> None:
        """Atomic write primary_meta.json to spawn_dir."""

        with self._metadata_lock:
            metadata = self._metadata.to_primary_metadata()
        write_primary_metadata(
            self._spawn_dir,
            metadata,
            runtime_root=self._runtime_root,
            spawn_id=str(self._spawn_id) if self._runtime_root is not None else None,
        )

    async def _consume_live_events(self) -> None:
        """Update activity and run inline hooks (fold, sinks) for each live event."""

        async for event in self._connection.events():
            self._update_activity_from_event(event)
            run_event_hooks(self._event_hooks, event)
        error = RuntimeError("managed primary event stream ended before launcher teardown")
        if self._idle_error_reporter is not None:
            self._idle_error_reporter.record(phase="event_stream", error=error)
        logger.warning(
            "primary_attach.observer_ended",
            spawn_id=str(self._spawn_id),
            error=str(error),
        )

    def _update_activity_from_event(self, event: RawHarnessEvent) -> None:
        """Update activity state based on connection events."""

        normalized = normalize_event(
            event,
            primary_event_scope=self._connection.primary_event_scope,
        )
        self._connection.observe_event_semantics(normalized.semantics)
        activity = normalized.semantics.activity
        if activity is not None:
            self._set_activity(activity)

    def _set_activity(self, activity: ActivityState) -> None:
        should_write = False
        with self._metadata_lock:
            if self._metadata.activity == "finalizing" and activity != "finalizing":
                return
            if self._metadata.activity != activity:
                self._metadata.activity = activity
                should_write = True
        if should_write:
            self._write_metadata()

    def _set_harness_session_id(self, session_id: str | None) -> None:
        if session_id is None:
            return
        if self._session_id_observer is not None:
            self._session_id_observer(session_id)
        should_write = False
        with self._metadata_lock:
            if (
                self._metadata.harness_session_id
                and self._metadata.harness_session_id != session_id
            ):
                return
            if self._metadata.harness_session_id != session_id:
                self._metadata.harness_session_id = session_id
                should_write = True
        if should_write:
            self._write_metadata()

    def _set_tui_pid(self, pid: int) -> None:
        should_write = False
        with self._metadata_lock:
            if self._metadata.tui_pid != pid:
                self._metadata.tui_pid = pid
                self._metadata.tui_birth_epoch = _process_birth_epoch(pid)
                should_write = True
        if should_write:
            self._write_metadata()

    def _record_backend_scope_from_connection(self, session_id: str | None) -> None:
        """Record backend scope using the connection's managed-backend handle.

        Managed observer mode records a provisional spawn_owned backend scope as
        soon as the process exists. This write upgrades that same concrete scope
        to session_owned once the harness session id is known.
        """
        backend_pid = self._connection.subprocess_pid
        if backend_pid is None or backend_pid <= 0:
            return

        scope_snapshot = self._connection.scope_snapshot
        if scope_snapshot is None:
            return

        session_key = (session_id or "").strip()
        owner_policy = "session_owned" if session_key else "spawn_owned"
        owner_id = session_key or str(self._spawn_id)
        if scope_snapshot.owner_policy != owner_policy or scope_snapshot.owner_id != owner_id:
            scope_snapshot = replace(
                scope_snapshot,
                owner_policy=owner_policy,
                owner_id=owner_id,
            )

        record_scope(self._spawn_dir.parent.parent, self._spawn_id, scope_snapshot)

    def _record_tui_scope(self, identity: ProcessIdentity, session_id: str | None) -> None:
        runtime_root = self._spawn_dir.parent.parent
        owner_id = (session_id or "").strip() or str(self._spawn_id)
        snapshot = scope_from_identity(
            identity,
            scope_id="tui",
            role="harness_tui",
            owner_id=owner_id,
            owner_policy="session_owned",
        )
        self._tui_scope_snapshot = snapshot
        record_scope(runtime_root, self._spawn_id, snapshot)

    async def _stop_tui_launch_task(
        self,
        running_process: RunningProcess,
        launch_task: asyncio.Future[LaunchedProcess],
        *,
        reason: str,
    ) -> None:
        await self._terminate_tui_scope(reason=reason)
        running_process.terminate()
        try:
            await asyncio.wait_for(
                asyncio.shield(launch_task),
                timeout=_TUI_LAUNCH_DRAIN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "primary_attach.tui_drain_timeout",
                spawn_id=str(self._spawn_id),
            )
            running_process.terminate()
            running_process.cancel_wait()
            try:
                await asyncio.wait_for(
                    asyncio.shield(launch_task),
                    timeout=_TUI_WAIT_CANCEL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.error(
                    "primary_attach.tui_wait_cancel_timeout",
                    spawn_id=str(self._spawn_id),
                )
                launch_task.cancel()
                with suppress(asyncio.CancelledError):
                    await launch_task
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _terminate_tui_scope(self, *, reason: str) -> None:
        snapshot = self._tui_scope_snapshot
        if snapshot is None:
            return
        await asyncio.to_thread(
            terminate_scope_sync,
            snapshot,
            grace_seconds=TUI_TERMINATE_GRACE_SECONDS,
            reason=reason,
        )

    def _resolve_backend_port(self) -> int | None:
        endpoint = self._connection.observer_endpoint
        if endpoint is None:
            return None
        return endpoint.port

    def _tui_env(self, env: dict[str, str]) -> dict[str, str]:
        """Merge the harness-provided attach-client env overlay for the TUI.

        The backend may require auth (e.g. V2's ``OPENCODE_PASSWORD``) that the
        attach client reads from its environment. The overlay applies to the TUI
        child only and is never written to spawn artifacts.
        """

        endpoint = self._connection.observer_endpoint
        if endpoint is None or not endpoint.client_env:
            return env
        return {**env, **dict(endpoint.client_env)}

    def _with_fresh_retry_port(self, config: ConnectionConfig) -> ConnectionConfig:
        return replace(
            config,
            ws_port=_reserve_local_port(host=config.ws_bind_host),
        )


def _reserve_local_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


__all__ = [
    "MAX_PORT_RETRY_ATTEMPTS",
    "ActivityState",
    "PortBindError",
    "PrimaryAttachError",
    "PrimaryAttachLauncher",
    "PrimaryAttachOutcome",
    "PrimaryMetadata",
    "TuiCommandBuilder",
]
