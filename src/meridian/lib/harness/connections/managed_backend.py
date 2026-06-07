"""Managed harness backend process lifecycle owner."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

import psutil

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.errors import HarnessBinaryNotFound
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.detached_process import (
    ParentDeathLink,
    detached_backend_subprocess_kwargs,
    link_child_lifetime_to_parent,
)
from meridian.lib.platform.process_scope import (
    ProcessScopeSnapshot,
    ScopedProcessHandle,
)
from meridian.lib.state.backend_lifecycle import (
    BackendLifecycleRecord,
    write_backend_lifecycle,
)
from meridian.lib.state.paths import resolve_spawn_log_dir
from meridian.lib.state.process_scope_projection import record_scope

_DEFAULT_LAUNCHING_TIMEOUT_SECONDS = 30.0


class BackendPhase(StrEnum):
    """Lifecycle phases with associated deadlines."""

    LAUNCHING = "launching"
    CONNECTING = "connecting"
    OBSERVING_SESSION = "observing"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ManagedBackendConfig:
    """Inputs for launching a managed backend process."""

    spawn_id: SpawnId
    harness_id: HarnessId
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    control_root: Path
    stderr_log_path: Path
    observer_mode: bool


@dataclass(frozen=True)
class PhaseDeadline:
    """One phase's timeout constraint."""

    phase: BackendPhase
    deadline_epoch: float
    timeout_seconds: float


@dataclass(frozen=True)
class ManagedBackendHandle:
    """Returned after successful launch. Immutable facts about the backend."""

    process: asyncio.subprocess.Process
    pid: int
    scope_snapshot: ProcessScopeSnapshot
    scope_handle: ScopedProcessHandle
    parent_death_link: ParentDeathLink


class ManagedBackend:
    """Single owner of managed backend process lifecycle facts."""

    def __init__(self, *, spawn_dir: Path) -> None:
        self._spawn_dir = spawn_dir
        self._runtime_root = spawn_dir.parent.parent
        self._current_phase = BackendPhase.LAUNCHING
        self._phase_entered_epoch = 0.0
        self._current_deadline: PhaseDeadline | None = None
        self._backend_pid: int | None = None
        self._backend_birth_epoch: float | None = None
        self._scope_snapshot: ProcessScopeSnapshot | None = None
        self._parent_death_linked = False
        self._harness_session_id: str | None = None

    @property
    def current_phase(self) -> BackendPhase:
        return self._current_phase

    @property
    def current_deadline(self) -> PhaseDeadline | None:
        return self._current_deadline

    @property
    def scope_snapshot(self) -> ProcessScopeSnapshot | None:
        return self._scope_snapshot

    def phase_expired(self) -> bool:
        """True if the current phase's deadline has passed."""
        deadline = self._current_deadline
        if deadline is None:
            return False
        return time.time() >= deadline.deadline_epoch

    def advance_phase(self, phase: BackendPhase, timeout_seconds: float) -> PhaseDeadline:
        """Record a phase transition and arm the deadline for the new phase."""

        entered_epoch = time.time()
        self._current_phase = phase
        self._phase_entered_epoch = entered_epoch
        deadline = PhaseDeadline(
            phase=phase,
            deadline_epoch=entered_epoch + max(timeout_seconds, 0.0),
            timeout_seconds=timeout_seconds,
        )
        self._current_deadline = deadline
        return deadline

    def persist_phase(self) -> None:
        """Atomically write current phase + pid + scope to the spawn sidecar."""

        if (
            self._backend_pid is None
            or self._backend_birth_epoch is None
            or self._scope_snapshot is None
            or self._current_deadline is None
        ):
            raise RuntimeError("ManagedBackend.persist_phase() called before launch completed")

        write_backend_lifecycle(
            self._spawn_dir,
            BackendLifecycleRecord(
                phase=self._current_phase.value,
                phase_entered_epoch=self._phase_entered_epoch,
                phase_timeout_seconds=self._current_deadline.timeout_seconds,
                backend_pid=self._backend_pid,
                backend_birth_epoch=self._backend_birth_epoch,
                scope_snapshot=self._scope_snapshot,
                harness_session_id=self._harness_session_id,
                parent_death_linked=self._parent_death_linked,
            ),
        )

    async def launch(
        self,
        config: ManagedBackendConfig,
        *,
        stderr: int | IO[Any],
    ) -> ManagedBackendHandle:
        """Launch subprocess, build scope snapshot, link parent death, record scope."""

        spawn_dir = resolve_spawn_log_dir(config.control_root, config.spawn_id)
        spawn_dir.mkdir(parents=True, exist_ok=True)
        self._spawn_dir = spawn_dir
        self._runtime_root = spawn_dir.parent.parent

        try:
            process = await asyncio.create_subprocess_exec(
                *config.command,
                cwd=str(config.cwd),
                env=config.env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=stderr,
                **detached_backend_subprocess_kwargs(),
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HarnessBinaryNotFound.from_os_error(
                harness_id=config.harness_id,
                error=exc,
                binary_name=config.command[0],
            ) from exc

        pid = process.pid
        pgid: int | None = None
        containment: str
        if not IS_WINDOWS:
            try:
                pgid = os.getpgid(pid)
                containment = "posix_pgid"
            except OSError:
                containment = "pid_tree_fallback"
        else:
            containment = "windows_job"

        try:
            birth_time = psutil.Process(pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            birth_time = time.time()

        snapshot = ProcessScopeSnapshot(
            scope_id="backend",
            owner_policy="spawn_owned",
            owner_id=str(config.spawn_id),
            role="harness_backend",
            containment=containment,
            root_pid=pid,
            root_created_at_epoch=birth_time,
            pgid=pgid,
            job_name=None,
            degraded_reason=None,
        )
        scope_handle = ScopedProcessHandle(process=process, snapshot=snapshot)
        parent_death_link = link_child_lifetime_to_parent(pid)

        if not config.observer_mode:
            record_scope(self._runtime_root, config.spawn_id, snapshot)

        self._backend_pid = pid
        self._backend_birth_epoch = birth_time
        self._scope_snapshot = snapshot
        self._parent_death_linked = not IS_WINDOWS or parent_death_link.job_handle is not None
        self._harness_session_id = None

        self.advance_phase(BackendPhase.LAUNCHING, _DEFAULT_LAUNCHING_TIMEOUT_SECONDS)
        self.persist_phase()

        return ManagedBackendHandle(
            process=process,
            pid=pid,
            scope_snapshot=snapshot,
            scope_handle=scope_handle,
            parent_death_link=parent_death_link,
        )


__all__ = [
    "BackendPhase",
    "ManagedBackend",
    "ManagedBackendConfig",
    "ManagedBackendHandle",
    "PhaseDeadline",
]
