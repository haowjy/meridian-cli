"""Spawn state query and shaping helpers backed by `spawns.jsonl`."""

import json
import re
from pathlib import Path
from typing import cast

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.ops.reference import resolve_spawn_ref
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn.model import SpawnRecord

from .models import SpawnDetailOutput

_SPAWN_REFERENCE_STATUS_FILTERS: dict[str, tuple[str, ...] | None] = {
    "@latest": None,
    "@last-failed": ("failed",),
    "@last-completed": ("succeeded",),
}
_RUNNING_LOG_MESSAGE_LIMIT = 120
_ASSISTANT_ROLE_MARKER_RE = re.compile(r"^(assistant|codex)$", re.IGNORECASE)
_LOG_ROLE_MARKER_RE = re.compile(r"^(user|assistant|codex|exec)$", re.IGNORECASE)


def _select_latest_spawn_id(
    project_root: Path,
    *,
    statuses: tuple[str, ...] | None,
    runtime_root: Path | None = None,
) -> str | None:
    from meridian.lib.state.reaper import reconcile_spawns

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    spawns = reconcile_spawns(
        project_root,
        resolved_runtime_root,
        spawn_store.list_spawns(resolved_runtime_root),
    )
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
            resolve_spawn_reference(project_root, ref, runtime_root=runtime_root)
            for ref in refs
        )
    )


def read_spawn_row(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> SpawnRecord | None:
    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    record = spawn_store.get_spawn(resolved_runtime_root, spawn_id)
    if record is not None and is_active_spawn_status(record.status):
        from meridian.lib.state.reaper import reconcile_active_spawn

        record = reconcile_active_spawn(project_root, resolved_runtime_root, record)
    return record


def read_spawn_row_read_only(
    project_root: Path,
    spawn_id: str,
    *,
    runtime_root: Path | None = None,
) -> SpawnRecord | None:
    """Return one spawn row without lifecycle reconciliation side effects."""

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    return spawn_store.get_spawn(resolved_runtime_root, spawn_id)


def read_latest_primary_spawn_for_chat_read_only(
    project_root: Path,
    chat_id: str,
    *,
    runtime_root: Path | None = None,
) -> SpawnRecord | None:
    """Return the latest primary spawn row for a chat without reconciliation."""

    resolved_runtime_root = runtime_root or resolve_runtime_root_for_read(project_root)
    spawns = spawn_store.list_spawns(resolved_runtime_root, filters={"chat_id": chat_id})
    primary_spawns = [row for row in spawns if row.kind == "primary"]
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
    stderr_path = resolved_runtime_root / "spawns" / spawn_id / "stderr.log"
    if not stderr_path.is_file():
        return stderr_path.as_posix(), None
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="ignore")
    return stderr_path.as_posix(), extract_last_assistant_message(stderr_text)


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
    artifacts = LocalStore(root_dir=resolved_runtime_root / "artifacts")
    return extract_written_files(artifacts, SpawnId(spawn_id))


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

    return SpawnDetailOutput(
        spawn_id=row.id,
        status=row.status,
        model=row.model or "",
        harness=row.harness or "",
        parent_id=row.parent_id,
        work_id=row.work_id,
        goal=row.goal,
        desc=row.desc,
        started_at=row.started_at or "",
        finished_at=row.finished_at,
        duration_secs=row.duration_secs,
        exit_code=row.exit_code,
        failure_reason=row.error,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_read_input_tokens=row.cache_read_input_tokens,
        cache_creation_input_tokens=row.cache_creation_input_tokens,
        reasoning_tokens=row.reasoning_tokens,
        cost_usd=row.total_cost_usd,
        cost_is_estimate=row.cost_is_estimate,
        report_path=report_path,
        report_summary=report_summary,
        report_body=report_body,
        harness_session_id=row.harness_session_id,
        last_message=last_message,
        log_path=log_path,
        exited_at=row.exited_at,
        process_exit_code=row.process_exit_code,
        claude_config_dir=row.claude_config_dir,
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
]
