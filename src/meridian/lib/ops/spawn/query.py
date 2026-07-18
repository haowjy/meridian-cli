"""Spawn state query and shaping helpers backed by per-spawn `state.json` (v2)."""

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, cast

from meridian.lib.core.depth import is_root_side_effect_process
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.harness.pi_lifecycle_events import PI_PHASE_EVENT_TYPE as _PI_PHASE_EVENT_TYPE
from meridian.lib.launch.constants import OUTPUT_FILENAME
from meridian.lib.ops.reference import resolve_spawn_ref
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state import session_identity, spawn_store
from meridian.lib.state.liveness import is_process_alive
from meridian.lib.state.reaper import (
    SPAWN_HEARTBEAT_WINDOW_SECS,
    SPAWN_POST_RUNNER_EXIT_FINALIZATION_GRACE_SECS,
    SPAWN_STARTUP_GRACE_SECS,
)
from meridian.lib.state.spawn.model import SpawnRecord, TerminalFacts

from .models import SpawnDetailOutput

_SPAWN_REFERENCE_STATUS_FILTERS: dict[str, tuple[str, ...] | None] = {
    "@latest": None,
    "@last-failed": ("failed",),
    "@last-completed": ("succeeded",),
}
_RUNNING_LOG_MESSAGE_LIMIT = 120
_ASSISTANT_ROLE_MARKER_RE = re.compile(r"^(assistant|codex)$", re.IGNORECASE)
_LOG_ROLE_MARKER_RE = re.compile(r"^(user|assistant|codex|exec)$", re.IGNORECASE)
_NESTED_READ_ACTIVITY_ARTIFACTS: tuple[str, ...] = (
    "heartbeat",
    "history.jsonl",
    OUTPUT_FILENAME,
    "bash-records.json",
    "stderr.log",
    "report.md",
)
_NESTED_READ_HEARTBEAT_WINDOW_SECS = SPAWN_HEARTBEAT_WINDOW_SECS
_NESTED_READ_STARTUP_GRACE_SECS = SPAWN_STARTUP_GRACE_SECS
_NESTED_READ_POST_RUNNER_EXIT_FINALIZATION_GRACE_SECS = (
    SPAWN_POST_RUNNER_EXIT_FINALIZATION_GRACE_SECS
)


class _PiCleanupTelemetry(NamedTuple):
    status: str | None
    phase: str | None
    reason: str | None
    error: str | None


def _iso_to_epoch(raw_value: str | None) -> float | None:
    normalized = (raw_value or "").strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _has_recent_spawn_activity(runtime_root: Path, spawn_id: str, now: float) -> bool:
    spawn_dir = runtime_root / "spawns" / spawn_id
    for artifact_name in _NESTED_READ_ACTIVITY_ARTIFACTS:
        try:
            mtime_epoch = (spawn_dir / artifact_name).stat().st_mtime
        except OSError:
            continue
        if now - mtime_epoch <= _NESTED_READ_HEARTBEAT_WINDOW_SECS:
            return True
    return False


def _runner_exit_terminal_update(record: SpawnRecord) -> dict[str, object]:
    facts = record.runner_exit
    assert facts is not None
    return {
        "status": facts.status,
        "terminal": TerminalFacts(
            exit_code=facts.exit_code,
            finished_at=facts.exited_at,
            published_at=facts.exited_at,
            error=facts.error,
            origin="runner",
        ),
    }


def _synthetic_stale_terminal_update(*, error: str, now: float) -> dict[str, object]:
    observed_at = datetime.fromtimestamp(now, tz=UTC).isoformat().replace("+00:00", "Z")
    return {
        "status": "failed",
        "terminal": TerminalFacts(
            exit_code=1,
            finished_at=observed_at,
            published_at=observed_at,
            error=error,
            origin="reconciler",
        ),
    }


def _read_only_nested_staleness_view(
    *,
    runtime_root: Path,
    record: SpawnRecord,
) -> SpawnRecord:
    now = time.time()
    started_epoch = _iso_to_epoch(record.started_at)
    in_startup_grace = (
        started_epoch is not None and now - started_epoch < _NESTED_READ_STARTUP_GRACE_SECS
    )

    if record.runner_exit is not None:
        exited_epoch = _iso_to_epoch(record.runner_exit.exited_at)
        if (
            exited_epoch is not None
            and now - exited_epoch < _NESTED_READ_POST_RUNNER_EXIT_FINALIZATION_GRACE_SECS
        ):
            return record
        return record.model_copy(update=_runner_exit_terminal_update(record))

    if _has_recent_spawn_activity(runtime_root, record.id, now):
        return record

    runner_pid = record.runner_pid
    if runner_pid is not None and runner_pid > 0:
        if in_startup_grace:
            return record
        runner_created_at_epoch = (
            record.runner_created_at_epoch
            if record.runner_created_at_epoch is not None
            else started_epoch
        )
        if is_process_alive(runner_pid, created_after_epoch=runner_created_at_epoch):
            return record
        return record.model_copy(
            update=_synthetic_stale_terminal_update(error="stale_nested_read", now=now)
        )

    if in_startup_grace:
        return record
    return record.model_copy(
        update=_synthetic_stale_terminal_update(error="stale_nested_read_no_pid", now=now)
    )


def _select_latest_spawn_id(
    project_root: Path,
    *,
    statuses: tuple[str, ...] | None,
    runtime_root: Path | None = None,
) -> str | None:
    from meridian.lib.state.reaper import reconcile_spawns

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return None
    spawns = reconcile_spawns(
        project_root,
        resolved_runtime_root,
        spawn_store.list_spawns(resolved_runtime_root),
    ).records
    if statuses is not None:
        wanted = set(statuses)
        spawns = [item for item in spawns if item.status in wanted]
    if not spawns:
        return None
    return spawns[-1].id


def resolve_spawn_reference(
    project_root: Path,
    ref: str,
    *,
    runtime_root: Path | None = None,
) -> str:
    normalized = ref.strip()
    if not normalized:
        raise ValueError("spawn_id is required")
    if not normalized.startswith("@"):
        resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
        if resolved_runtime_root is None:
            return normalized
        resolved = resolve_spawn_ref(resolved_runtime_root, normalized)
        return str(resolved) if resolved is not None else normalized

    status_filter = _SPAWN_REFERENCE_STATUS_FILTERS.get(normalized)
    if normalized not in _SPAWN_REFERENCE_STATUS_FILTERS:
        supported = ", ".join(sorted(_SPAWN_REFERENCE_STATUS_FILTERS))
        raise ValueError(
            f"Unknown spawn reference '{normalized}'. Supported references: {supported}"
        )

    resolved = _select_latest_spawn_id(
        project_root,
        statuses=status_filter,
        runtime_root=runtime_root,
    )
    if resolved is None:
        raise ValueError(f"No spawns found for reference '{normalized}'")
    return resolved


def resolve_spawn_references(
    project_root: Path,
    refs: tuple[str, ...],
    *,
    runtime_root: Path | None = None,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            resolve_spawn_reference(project_root, ref, runtime_root=runtime_root) for ref in refs
        )
    )


def read_spawn_row(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> SpawnRecord | None:
    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return None
    record = spawn_store.get_spawn(resolved_runtime_root, spawn_id)
    if record is not None and is_active_spawn_status(record.status):
        if is_root_side_effect_process():
            from meridian.lib.state.reaper import peek_reconciled_active_spawn

            record = peek_reconciled_active_spawn(resolved_runtime_root, record)
        else:
            record = _read_only_nested_staleness_view(
                runtime_root=resolved_runtime_root,
                record=record,
            )
    return record


def read_spawn_row_read_only(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> SpawnRecord | None:
    """Return one spawn row without lifecycle reconciliation side effects."""

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return None
    return spawn_store.get_spawn(resolved_runtime_root, spawn_id)


def read_latest_primary_spawn_for_chat_read_only(
    project_root: Path,
    chat_id: str,
    *,
    runtime_root: Path | None = None,
) -> SpawnRecord | None:
    """Return the latest primary spawn row for a chat without reconciliation."""

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return None
    spawns = session_identity.list_spawns_for_owner_chat(resolved_runtime_root, chat_id)
    primary_spawns = [row for row in spawns.records if row.kind == "primary"]
    if not primary_spawns:
        return None
    return primary_spawns[-1]


def read_report(
    project_root: Path,
    spawn_id: str,
    *,
    include_body: bool,
    runtime_root: Path | None = None,
) -> tuple[str | None, str | None]:
    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return None, None
    report_path = resolved_runtime_root / "spawns" / spawn_id / "report.md"
    if not report_path.is_file():
        return None, None
    if not include_body:
        return report_path.as_posix(), None
    text = report_path.read_text(encoding="utf-8", errors="ignore").strip() or None
    return report_path.as_posix(), text


def read_report_text(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> tuple[str | None, str | None]:
    return read_report(project_root, spawn_id, include_body=True, runtime_root=runtime_root)


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


def _read_running_log_details(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> tuple[str, str | None]:
    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return "", None
    stderr_path = resolved_runtime_root / "spawns" / spawn_id / "stderr.log"
    if not stderr_path.is_file():
        return stderr_path.as_posix(), None
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="ignore")
    return stderr_path.as_posix(), extract_last_assistant_message(stderr_text)


def _latest_pi_lifecycle_phase(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> str | None:
    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return None
    history_path = resolved_runtime_root / "spawns" / spawn_id / "history.jsonl"
    if not history_path.is_file():
        return None

    last_phase: str | None = None
    for line in history_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw_payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw_payload, dict):
            continue
        payload = cast("dict[str, object]", raw_payload)
        if payload.get("event_type") != _PI_PHASE_EVENT_TYPE:
            continue
        raw_event_payload = payload.get("payload")
        if not isinstance(raw_event_payload, dict):
            continue
        event_payload = cast("dict[str, object]", raw_event_payload)
        phase_value = event_payload.get("phase")
        if isinstance(phase_value, str) and phase_value.strip():
            last_phase = phase_value.strip()
    return last_phase


def _pi_cleanup_telemetry(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> _PiCleanupTelemetry:
    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return _PiCleanupTelemetry(None, None, None, None)
    history_path = resolved_runtime_root / "spawns" / spawn_id / "history.jsonl"
    if not history_path.is_file():
        return _PiCleanupTelemetry(None, None, None, None)

    status_rank: dict[str, int] = {"running": 0, "completed": 1, "escalated": 2, "failed": 3}
    cleanup_status: str | None = None
    cleanup_phase: str | None = None
    cleanup_reason: str | None = None
    cleanup_error: str | None = None

    for line in history_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw_payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw_payload, dict):
            continue
        payload = cast("dict[str, object]", raw_payload)
        if payload.get("event_type") != _PI_PHASE_EVENT_TYPE:
            continue
        raw_event_payload = payload.get("payload")
        if not isinstance(raw_event_payload, dict):
            continue
        event_payload = cast("dict[str, object]", raw_event_payload)
        phase_value = event_payload.get("phase")
        phase = (
            phase_value.strip()
            if isinstance(phase_value, str) and phase_value.strip()
            else None
        )
        status_value = event_payload.get("cleanup_status")
        status = (
            status_value.strip()
            if isinstance(status_value, str) and status_value.strip()
            else None
        )
        if phase is None and status is None:
            continue
        if phase is not None and not phase.startswith("cleanup_") and status is None:
            continue

        if phase is not None:
            cleanup_phase = phase
            if phase == "cleanup_escalated":
                status = "escalated"
            elif phase == "cleanup_failed":
                status = "failed"

        if status is not None:
            prior_rank = status_rank.get(cleanup_status or "", -1)
            current_rank = status_rank.get(status, -1)
            if current_rank >= prior_rank:
                cleanup_status = status

        reason_value = event_payload.get("reason")
        if isinstance(reason_value, str) and reason_value.strip():
            cleanup_reason = reason_value.strip()
        error_value = event_payload.get("error")
        if isinstance(error_value, str) and error_value.strip():
            cleanup_error = error_value.strip()

    return _PiCleanupTelemetry(
        status=cleanup_status,
        phase=cleanup_phase,
        reason=cleanup_reason,
        error=cleanup_error,
    )


def read_written_files(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> tuple[str, ...]:
    from meridian.lib.core.types import SpawnId
    from meridian.lib.launch.written_files import extract_written_files
    from meridian.lib.state.artifact_store import LocalStore

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    if resolved_runtime_root is None:
        return ()
    artifacts = LocalStore(root_dir=resolved_runtime_root / "artifacts")
    return extract_written_files(artifacts, SpawnId(spawn_id))


def spawn_session_log_available(
    runtime_root: Path,
    spawn_id: str,
    *,
    harness_session_id: str | None = None,
) -> bool:
    """Return whether ``meridian session log`` can resolve content for one spawn."""
    if (harness_session_id or "").strip():
        return True
    history_path = runtime_root / "spawns" / spawn_id / "history.jsonl"
    return history_path.is_file()


def detail_from_row(
    *,
    project_root: Path,
    row: SpawnRecord,
    include_report_body: bool,
    runtime_root: Path | None = None,
) -> SpawnDetailOutput:
    report_path, report_body = read_report(
        project_root,
        row.id,
        include_body=include_report_body,
        runtime_root=runtime_root,
    )
    report_summary = report_body[:500] if report_body else None

    last_message: str | None = None
    log_path: str | None = None
    if is_active_spawn_status(row.status):
        log_path, last_message = _read_running_log_details(
            project_root,
            row.id,
            runtime_root=runtime_root,
        )
    cleanup_telemetry = _pi_cleanup_telemetry(
        project_root,
        row.id,
        runtime_root=runtime_root,
    )

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)

    terminal = row.terminal
    return SpawnDetailOutput(
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
        duration_secs=terminal.duration_secs if terminal is not None else None,
        exit_code=terminal.exit_code if terminal is not None else None,
        failure_reason=terminal.error if terminal is not None else None,
        input_tokens=terminal.input_tokens if terminal is not None else None,
        output_tokens=terminal.output_tokens if terminal is not None else None,
        cache_read_input_tokens=terminal.cache_read_input_tokens if terminal is not None else None,
        cache_creation_input_tokens=(
            terminal.cache_creation_input_tokens if terminal is not None else None
        ),
        reasoning_tokens=terminal.reasoning_tokens if terminal is not None else None,
        cost_usd=terminal.total_cost_usd if terminal is not None else None,
        cost_is_estimate=terminal.cost_is_estimate if terminal is not None else False,
        report_path=report_path,
        report_summary=report_summary,
        report_body=report_body,
        harness_session_id=row.harness_session_id,
        pi_lifecycle_phase=_latest_pi_lifecycle_phase(
            project_root,
            row.id,
            runtime_root=runtime_root,
        ),
        pi_cleanup_status=cleanup_telemetry.status,
        pi_cleanup_phase=cleanup_telemetry.phase,
        pi_cleanup_reason=cleanup_telemetry.reason,
        pi_cleanup_error=cleanup_telemetry.error,
        last_message=last_message,
        log_path=log_path,
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
    "detail_from_row",
    "extract_last_assistant_message",
    "read_report",
    "read_report_text",
    "read_spawn_row",
    "read_written_files",
    "resolve_spawn_reference",
    "resolve_spawn_references",
    "spawn_session_log_available",
]
