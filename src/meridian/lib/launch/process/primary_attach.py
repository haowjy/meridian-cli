"""Primary attach launcher for managed-backend primary sessions.

Orchestrates: backend connection (owner: connection class) + TUI subprocess + metadata sidecar.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
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
from meridian.lib.platform.process_scope import terminate_scope_sync
from meridian.lib.platform.process_scope.base import (
    PROCESS_BIRTH_UNKNOWN_EPOCH,
    ProcessScopeSnapshot,
)
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
        birth_time = PROCESS_BIRTH_UNKNOWN_EPOCH

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


def _process_birth_epoch(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None


@dataclass
class _LauncherMetadata:
    """Mutable working copy for launcher writes."""

    managed_backend: bool = True
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
        self._tui_scope_snapshot: ProcessScopeSnapshot | None = None

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
                self._metadata.backend_birth_epoch = (
                    _process_birth_epoch(self._connection.subprocess_pid)
                    if self._connection.subprocess_pid is not None
                    else None
                )
                self._metadata.backend_port = self._resolve_backend_port()
            self._write_metadata()

            self._record_backend_scope_from_connection(session_id)

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
                self._record_tui_scope(pid, session_id)
                if running_callback is not None:
                    running_callback(pid)

            def _on_child_started(pid: int) -> None:
                loop.call_soon_threadsafe(_handle_running, pid)

            launch_task = asyncio.create_task(
                asyncio.to_thread(
                    self._process_launcher.launch,
                    command=command,
                    cwd=cwd,
                    env=env,
                    output_log_path=None,
                    on_child_started=_on_child_started,
                )
            )
            telemetry.clear()

            writer_task = self._event_writer_task
            done, _pending = await asyncio.wait(
                {launch_task, writer_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if writer_task in done and not launch_task.done():
                with suppress(asyncio.CancelledError, Exception):
                    writer_task.result()
                self._event_writer_task = None
                await self._connection.stop(reason="event_stream_closed")
                await self._terminate_tui_scope()
                launched = await launch_task
                return PrimaryAttachOutcome(
                    exit_code=1,
                    session_id=session_id,
                    tui_pid=launched.pid,
                )

            launched = await launch_task

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

        main_thread_id = getattr(self._connection, "main_turn_thread_id", None)
        codex_main_thread_id = (
            main_thread_id if isinstance(main_thread_id, str) and main_thread_id.strip() else None
        )
        activity = activity_transition(event, codex_main_thread_id=codex_main_thread_id)
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

    def _record_tui_scope(self, pid: int, session_id: str | None) -> None:
        if pid <= 0:
            return
        runtime_root = self._spawn_dir.parent.parent
        owner_id = (session_id or "").strip() or str(self._spawn_id)
        snapshot = _make_scope_snapshot(
            pid=pid,
            scope_id="tui",
            owner_policy="session_owned",
            owner_id=owner_id,
            role="harness_tui",
        )
        self._tui_scope_snapshot = snapshot
        record_scope(runtime_root, self._spawn_id, snapshot)

    async def _terminate_tui_scope(self) -> None:
        snapshot = self._tui_scope_snapshot
        if snapshot is None:
            return
        await asyncio.to_thread(
            terminate_scope_sync,
            snapshot,
            grace_seconds=5.0,
            reason="event_stream_closed",
        )

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
