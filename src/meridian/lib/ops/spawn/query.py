"""Spawn state query and shaping helpers backed by per-spawn `state.json`."""

import json
import re
from pathlib import Path
from typing import NamedTuple, cast

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.ops.run_boundary import run_boundary_summary
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state import pi_lifecycle, session_store, spawn_store
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.permission_journal import PermissionJournal
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn_status import read_derived

from .models import DeniedRequestOutput, SpawnDetailOutput

_RUNNING_LOG_MESSAGE_LIMIT = 120
_ASSISTANT_ROLE_MARKER_RE = re.compile(r"^(assistant|codex)$", re.IGNORECASE)
_LOG_ROLE_MARKER_RE = re.compile(r"^(user|assistant|codex|exec)$", re.IGNORECASE)
class _PiCleanupTelemetry(NamedTuple):
    status: str | None
    phase: str | None
    reason: str | None
    error: str | None


def read_spawn_row(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> SpawnRecord | None:
    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return None
    return read_derived(resolved_runtime_root, spawn_id)


def read_stored_spawn_row(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> SpawnRecord | None:
    """Return persisted status without deriving liveness.

    Transcript capture needs a stored terminal outcome; a derived crash cannot be
    archived before doctor or cancel persists it.
    """

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return None
    return spawn_store.get_spawn(resolved_runtime_root, spawn_id)


def read_report(launch: LaunchPaths | None) -> str | None:
    """Return a report's stripped body; ``None`` launch means no runtime root."""

    if launch is None or not launch.report.is_file():
        return None
    return launch.report.read_text(encoding="utf-8", errors="ignore").strip() or None


def read_report_summary(launch: LaunchPaths | None) -> str | None:
    """Read at most the byte window needed for a 500-character report prefix."""
    if launch is None or not launch.report.is_file():
        return None
    # A UTF-8 code point uses at most four bytes. Ignoring an incomplete trailing
    # code point makes a byte-window read safe without reading the rest of the file.
    with launch.report.open("rb") as report_file:
        prefix = report_file.read(500 * 4)
    return prefix.decode("utf-8", errors="ignore").strip()[:500] or None


def _truncate_log_message(value: str, *, max_chars: int = _RUNNING_LOG_MESSAGE_LIMIT) -> str:
    compact = " ".join(value.split()).strip()
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 3].rstrip()}..."


def _log_text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_log_text_from_value(item) for item in cast("list[object]", value)]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        payload = cast("dict[str, object]", value)
        parts: list[str] = []
        for key in ("text", "message", "output", "content"):
            if key in payload:
                text = _log_text_from_value(payload[key])
                if text:
                    parts.append(text)
        return " ".join(parts).strip()
    return ""


def _assistant_texts(payload: object) -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        obj = cast("dict[str, object]", payload)
        role = str(obj.get("role", "")).lower()
        event_type = str(obj.get("type", obj.get("event", ""))).lower()
        category = str(obj.get("category", "")).lower()
        if role == "assistant" or "assistant" in event_type or category == "assistant":
            text = _log_text_from_value(obj)
            if text:
                found.append(text)
        for nested in obj.values():
            found.extend(_assistant_texts(nested))
        return found
    if isinstance(payload, list):
        for item in cast("list[object]", payload):
            found.extend(_assistant_texts(item))
    return found


def extract_last_assistant_message(stderr_text: str) -> str | None:
    last_message: str | None = None
    pending_assistant_lines: list[str] | None = None

    def _flush_pending_assistant() -> None:
        nonlocal last_message, pending_assistant_lines
        if pending_assistant_lines is None:
            return
        candidate = " ".join(line for line in pending_assistant_lines if line).strip()
        if candidate:
            last_message = candidate
        pending_assistant_lines = None

    for line in stderr_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if _ASSISTANT_ROLE_MARKER_RE.fullmatch(stripped):
            _flush_pending_assistant()
            pending_assistant_lines = []
            continue

        if pending_assistant_lines is not None:
            if _LOG_ROLE_MARKER_RE.fullmatch(stripped):
                _flush_pending_assistant()
                if _ASSISTANT_ROLE_MARKER_RE.fullmatch(stripped):
                    pending_assistant_lines = []
                continue
            pending_assistant_lines.append(stripped)
            continue

        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        assistant_texts = _assistant_texts(payload)
        if assistant_texts:
            last_message = assistant_texts[-1]
    _flush_pending_assistant()
    if last_message is None:
        return None
    return _truncate_log_message(last_message)


def _read_running_last_message(launch: LaunchPaths | None) -> str | None:
    if launch is None or not launch.stderr_log.is_file():
        return None
    return extract_last_assistant_message(
        launch.stderr_log.read_text(encoding="utf-8", errors="ignore")
    )


def _latest_pi_lifecycle_phase(launch: LaunchPaths | None) -> str | None:
    if launch is None:
        return None
    return pi_lifecycle.read(launch).phase


def _pi_cleanup_telemetry(launch: LaunchPaths | None) -> _PiCleanupTelemetry:
    if launch is None:
        return _PiCleanupTelemetry(None, None, None, None)
    lifecycle = pi_lifecycle.read(launch)
    return _PiCleanupTelemetry(
        status=lifecycle.cleanup_status,
        phase=lifecycle.cleanup_phase,
        reason=lifecycle.reason,
        error=lifecycle.error,
    )


def denied_requests(launch: LaunchPaths) -> tuple[DeniedRequestOutput, ...]:
    """The calls the spawn's model made that were refused, from its permission journal."""

    journal = PermissionJournal(launch)
    return tuple(
        DeniedRequestOutput(
            request_id=record.request_id,
            method=record.method,
            summary=record.summary,
            reason=record.reason,
        )
        for record in journal.denied_requests()
    )


def spawn_session_log_available(
    runtime_root: Path,
    spawn_id: str,
    *,
    harness_session_id: str | None = None,
) -> bool:
    """Return whether ``meridian session log`` can resolve content for one spawn."""
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    if row is None or row.continue_chat_id is None:
        return False
    record = session_store.chat_record(runtime_root, row.continue_chat_id)
    return record is not None and record.native_key() is not None


def detail_from_row(
    *,
    project_root: Path,
    row: SpawnRecord,
    include_report_body: bool,
    runtime_root: Path | None = None,
) -> SpawnDetailOutput:
    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    launch = (
        None if resolved_runtime_root is None else LaunchPaths.for_row(resolved_runtime_root, row)
    )
    if include_report_body:
        report_body = read_report(launch)
        report_summary = report_body[:500] if report_body else None
    else:
        report_summary = read_report_summary(launch)
        report_body = None

    last_message: str | None = None
    if is_active_spawn_status(row.status):
        last_message = _read_running_last_message(launch)
    cleanup_telemetry = _pi_cleanup_telemetry(launch)

    terminal = row.terminal
    return SpawnDetailOutput(
        boundary_summary=run_boundary_summary(row),
        chat_id=row.entry_chat_id,
        continue_chat_id=row.continue_chat_id,
        run_boundary=row.run_boundary,
        spawn_id=row.id,
        status=row.status,
        model=row.model or "",
        harness=row.harness or "",
        parent_id=row.parent_id,
        work_id=row.work_id,
        authority_root=row.control_root,
        task_cwd=row.task_cwd,
        goal=row.goal,
        desc=row.desc,
        started_at=row.started_at or "",
        finished_at=terminal.finished_at if terminal is not None else None,
        duration_secs=row.total_duration_secs,
        exit_code=terminal.exit_code if terminal is not None else None,
        failure_reason=terminal.error if terminal is not None else None,
        input_tokens=row.total_input_tokens,
        output_tokens=row.total_output_tokens,
        cache_read_input_tokens=terminal.cache_read_input_tokens if terminal is not None else None,
        cache_creation_input_tokens=(
            terminal.cache_creation_input_tokens if terminal is not None else None
        ),
        reasoning_tokens=terminal.reasoning_tokens if terminal is not None else None,
        cost_usd=row.total_cost_usd,
        cost_is_estimate=terminal.cost_is_estimate if terminal is not None else False,
        report_summary=report_summary,
        report_body=report_body,
        harness_session_id=row.harness_session_id,
        pi_lifecycle_phase=_latest_pi_lifecycle_phase(launch),
        pi_cleanup_status=cleanup_telemetry.status,
        pi_cleanup_phase=cleanup_telemetry.phase,
        pi_cleanup_reason=cleanup_telemetry.reason,
        pi_cleanup_error=cleanup_telemetry.error,
        last_message=last_message,
        last_attempt_exited_at=row.last_attempt_exited_at,
        last_attempt_exit_code=row.last_attempt_exit_code,
        session_config_dir=row.claude_config_dir,
        session_log_available=(
            False
            if resolved_runtime_root is None
            else spawn_session_log_available(
                resolved_runtime_root,
                row.id,
                harness_session_id=row.harness_session_id,
            )
        ),
    )


__all__ = [
    "denied_requests",
    "detail_from_row",
    "extract_last_assistant_message",
    "read_report",
    "read_spawn_row",
    "spawn_session_log_available",
]
