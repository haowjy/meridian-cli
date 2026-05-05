"""Process launch package with compatibility wrappers."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from meridian.lib.platform import fcntl, termios
from meridian.lib.state.primary_meta import ActivityState, PrimaryMetadata
from meridian.lib.state.session_store import (
    get_session_active_work_id,
    start_session,
    stop_session,
    update_session_harness_id,
    update_session_work_id,
)

from .ports import ChildStartedHook, LaunchedProcess, ProcessLauncher
from .primary_attach import (
    MAX_PORT_RETRY_ATTEMPTS,
    PortBindError,
    PrimaryAttachError,
    PrimaryAttachLauncher,
    PrimaryAttachOutcome,
    TuiCommandBuilder,
)
from .pty_launcher import can_use_pty
from .runner import ProcessOutcome
from .runner import run_harness_process as _run_harness_process_impl
from .runner import run_primary_attach as _run_primary_attach_impl
from .runner import run_primary_process_with_capture as _run_primary_process_with_capture_impl


def _run_primary_process_with_capture(
    *,
    command: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    output_log_path: Path | None,
    on_child_started: ChildStartedHook | None = None,
) -> tuple[int, int | None]:
    return _run_primary_process_with_capture_impl(
        command,
        cwd,
        env,
        output_log_path,
        on_child_started,
    )


def _compat_run_primary(
    command: tuple[str, ...],
    cwd: Path,
    env: dict[str, str],
    output_log_path: Path | None,
    on_child_started: ChildStartedHook | None = None,
) -> tuple[int, int | None]:
    """Positional-args wrapper for test compatibility."""
    return _run_primary_process_with_capture(
        command=command,
        cwd=cwd,
        env=env,
        output_log_path=output_log_path,
        on_child_started=on_child_started,
    )


def _run_primary_attach(
    *,
    harness_id: Any,
    spawn_id: Any,
    spawn_dir: Path,
    execution_cwd: Path,
    env: dict[str, str],
    spec: Any,
    process_launcher: ProcessLauncher,
    on_running: ChildStartedHook | None = None,
) -> PrimaryAttachOutcome:
    return _run_primary_attach_impl(
        harness_id,
        spawn_id,
        spawn_dir,
        execution_cwd,
        env,
        spec,
        process_launcher,
        on_running,
    )


def _compat_run_primary_attach(
    harness_id: Any,
    spawn_id: Any,
    spawn_dir: Path,
    execution_cwd: Path,
    env: dict[str, str],
    spec: Any,
    process_launcher: ProcessLauncher,
    on_running: ChildStartedHook | None = None,
) -> PrimaryAttachOutcome:
    return _run_primary_attach(
        harness_id=harness_id,
        spawn_id=spawn_id,
        spawn_dir=spawn_dir,
        execution_cwd=execution_cwd,
        env=env,
        spec=spec,
        process_launcher=process_launcher,
        on_running=on_running,
    )


def run_harness_process(
    launch_context: Any,
    harness_registry: Any,
    *,
    prepared: Any = None,
    cache: Any = None,
) -> Any:
    # Preserve monkeypatch seams currently exercised in tests.
    return _run_harness_process_impl(
        launch_context,
        harness_registry,
        prepared=prepared,
        cache=cache,
        run_primary_process_with_capture_fn=_compat_run_primary,
        run_primary_attach_fn=_compat_run_primary_attach,
        start_session_fn=start_session,
        stop_session_fn=stop_session,
        update_session_harness_id_fn=update_session_harness_id,
        update_session_work_id_fn=update_session_work_id,
        get_session_active_work_id_fn=get_session_active_work_id,
    )


__all__ = [
    "MAX_PORT_RETRY_ATTEMPTS",
    "ActivityState",
    "ChildStartedHook",
    "LaunchedProcess",
    "PortBindError",
    "PrimaryAttachError",
    "PrimaryAttachLauncher",
    "PrimaryAttachOutcome",
    "PrimaryMetadata",
    "ProcessLauncher",
    "ProcessOutcome",
    "TuiCommandBuilder",
    "can_use_pty",
    "fcntl",
    "run_harness_process",
    "struct",
    "termios",
]
