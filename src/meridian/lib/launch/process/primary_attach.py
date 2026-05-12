"""Primary attach launcher for managed-backend primary sessions.

Orchestrates: backend connection (owner: connection class) + TUI subprocess + metadata sidecar.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock
from typing import Any

import psutil

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, HarnessConnection, HarnessEvent
from meridian.lib.harness.connections.errors import PortBindError
from meridian.lib.harness.semantics import activity_transition
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
from meridian.lib.state.history import HarnessHistoryWriter
from meridian.lib.state.primary_meta import ActivityState, PrimaryMetadata, write_primary_metadata
from meridian.lib.state.process_scope_projection import record_scope

from .ports import ProcessLauncher

TuiCommandBuilder = Callable[[str], tuple[str, ...]]
MAX_PORT_RETRY_ATTEMPTS = 3


def _make_scope_snapshot(
    pid: int,
    scope_id: str,
    owner_policy: str,
    owner_id: str,
    role: str,
) -> ProcessScopeSnapshot:
    """Build a ProcessScopeSnapshot for a managed-primary process.

    Determines containment type (posix_pgid / windows_job / pid_tree_fallback)
    and reads the process birth time via psutil for the PID-reuse guard.
    """
    try:
        birth_time = psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        birth_time = time.time()

    pgid: int | None = None
    containment: str
    if sys.platform != "win32":
        try:
            pgid = os.getpgid(pid)
            containment = "posix_pgid"
        except OSError:
            containment = "pid_tree_fallback"
    else:
        containment = "windows_job"

    return ProcessScopeSnapshot(
        scope_id=scope_id,
        owner_policy=owner_policy,
        owner_id=owner_id,
        role=role,
        containment=containment,
        root_pid=pid,
        root_created_at_epoch=birth_time,
        pgid=pgid,
        job_name=None,
        degraded_reason=None,
    )


class PrimaryAttachError(Exception):
    """Managed backend startup failed; caller should fall back to black-box path."""


def _try_assign_backend_job(pid: int) -> object:
    """Assign *pid* to a Windows Job Object and return the handle.

    The returned handle must remain alive for ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
    to take effect — store it for the lifetime of the launcher.

    Returns ``None`` on POSIX, on assignment failure, or on any exception so
    the caller always degrades cleanly without crashing the launch.
    """
    if not IS_WINDOWS:
        return None
    try:
        from meridian.lib.platform import windows_job

        result = windows_job.assign_to_new_job(pid)
        if result is None:
            return None
        _job_name, job_handle = result
        return job_handle
    except Exception:
        return None


@dataclass
class _LauncherMetadata:
    """Mutable working copy for launcher writes."""

    managed_backend: bool = True
    launcher_pid: int = field(default_factory=os.getpid)
    backend_pid: int | None = None
    tui_pid: int | None = None
    backend_port: int | None = None
    activity: ActivityState = "starting"
    harness_session_id: str | None = None

    def to_primary_metadata(self) -> PrimaryMetadata:
        return PrimaryMetadata(
            managed_backend=self.managed_backend,
            launcher_pid=self.launcher_pid,
            backend_pid=self.backend_pid,
            tui_pid=self.tui_pid,
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
        on_running: Callable[[int], None] | None = None,
    ) -> None:
        self._spawn_id = spawn_id
        self._spawn_dir = spawn_dir
        self._connection = connection
        self._tui_command_builder = tui_command_builder
        self._process_launcher = process_launcher
        self._on_running = on_running
        self._metadata = _LauncherMetadata()
        self._metadata_lock = Lock()
        self._history_writer: HarnessHistoryWriter | None = None
        self._event_writer_task: asyncio.Task[None] | None = None
        # Kept alive so KILL_ON_JOB_CLOSE terminates the backend on launcher exit.
        self._backend_job_handle: object = None

    async def run(
        self,
        config: ConnectionConfig,
        spec: ResolvedLaunchSpec,
        cwd: Path,
        env: dict[str, str],
        on_running: Callable[[int], None] | None = None,
    ) -> PrimaryAttachOutcome:
        """Execute the full primary attach lifecycle."""

        self._spawn_dir.mkdir(parents=True, exist_ok=True)
        self._history_writer = HarnessHistoryWriter(self._spawn_dir / "history.jsonl")
        connection_started = False
        session_id: str | None = None
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
                self._metadata.backend_port = self._resolve_backend_port()
            self._write_metadata()

            # Record backend scope. session_owned when session_id is available
            # (normal path) so the backend is preserved across launcher restarts.
            # Falls back to spawn_owned when session_id is absent (error path)
            # so normal spawn teardown terminates the orphaned backend process.
            _backend_pid = self._connection.subprocess_pid
            if _backend_pid is not None and _backend_pid > 0:
                _runtime_root = self._spawn_dir.parent.parent
                _sid = (session_id or "").strip()
                _policy = "session_owned" if _sid else "spawn_owned"
                _owner_id = _sid or str(self._spawn_id)
                record_scope(
                    _runtime_root,
                    self._spawn_id,
                    _make_scope_snapshot(
                        pid=_backend_pid,
                        scope_id="backend",
                        owner_policy=_policy,
                        owner_id=_owner_id,
                        role="harness_backend",
                    ),
                )
                self._backend_job_handle = _try_assign_backend_job(_backend_pid)

            self._event_writer_task = asyncio.create_task(self._run_event_writer())
            self._set_harness_session_id(session_id)
            self._set_activity("idle")

            if session_id is None or not session_id.strip():
                raise RuntimeError(
                    f"Managed primary attach requires a harness session id "
                    f"(spawn_id={self._spawn_id})"
                )

            telemetry.update(f"Attaching {self._connection.harness_id.value.capitalize()} TUI...")
            command = tuple(self._tui_command_builder(session_id))
            loop = asyncio.get_running_loop()
            running_callback = on_running if on_running is not None else self._on_running

            def _handle_running(pid: int) -> None:
                self._set_tui_pid(pid)
                if running_callback is not None:
                    running_callback(pid)

            def _on_child_started(pid: int) -> None:
                loop.call_soon_threadsafe(_handle_running, pid)

            launched = await asyncio.to_thread(
                self._process_launcher.launch,
                command=command,
                cwd=cwd,
                env=env,
                output_log_path=None,
                on_child_started=_on_child_started,
            )
            telemetry.clear()

            # Record TUI as session_owned — preserved across launcher death.
            _tui_pid = launched.pid
            if _tui_pid is not None and _tui_pid > 0:
                _runtime_root = self._spawn_dir.parent.parent
                _owner_id = (session_id or "").strip() or str(self._spawn_id)
                record_scope(
                    _runtime_root,
                    self._spawn_id,
                    _make_scope_snapshot(
                        pid=_tui_pid,
                        scope_id="tui",
                        owner_policy="session_owned",
                        owner_id=_owner_id,
                        role="harness_tui",
                    ),
                )

            return PrimaryAttachOutcome(
                exit_code=launched.exit_code,
                session_id=session_id,
                tui_pid=launched.pid,
            )
        except Exception:
            telemetry.fail()
            raise
        finally:
            if connection_started:
                self._set_activity("finalizing")
            writer_task = self._event_writer_task
            if writer_task is not None:
                writer_task.cancel()
                with suppress(asyncio.CancelledError):
                    await writer_task
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
                        f"{MAX_PORT_RETRY_ATTEMPTS} attempts; falling back to black-box launch"
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
        write_primary_metadata(self._spawn_dir, metadata)

    async def _run_event_writer(self) -> None:
        """Stream connection events to history.jsonl."""

        writer = self._history_writer
        if writer is None:
            raise RuntimeError("primary attach history writer is not initialized")
        try:
            async for event in self._connection.events():
                self._update_activity_from_event(event)
                writer.write(event)
        finally:
            self._history_writer = None

    def _update_activity_from_event(self, event: HarnessEvent) -> None:
        """Update activity state based on connection events."""

        activity = activity_transition(event)
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
        should_write = False
        with self._metadata_lock:
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
                should_write = True
        if should_write:
            self._write_metadata()

    def _resolve_backend_port(self) -> int | None:
        endpoint = self._connection.observer_endpoint
        if endpoint is None:
            return None
        return endpoint.port

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
