"""Qualify a native transcript observation for sealed snapshot publication.

Provider-owned grammar: complete, known-incomplete, unavailable, or unsupported.
Publication happens only after a complete observation of the declared scope.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from meridian.lib.harness.opencode_transcript import iter_opencode_db_events
from meridian.lib.harness.semantics import PI_INCOMPLETE_STOP_REASONS
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.native_snapshot import (
    SnapshotObservation,
    SnapshotRecord,
    SourceRevision,
    read_jsonl_frame,
    reject_unframed_storage_frame,
)

CaptureStatus = Literal["complete", "known-incomplete", "unavailable", "unsupported"]

_DIALECT = {
    "pi": "pi.session",
    "claude": "claude.jsonl",
    "codex": "codex.rollout",
    "opencode": "opencode.transcript.v1",
}
_SCOPE = "native-session"
_OPENCODE_PENDING = frozenset({"pending", "running"})
_CLAUDE_ERROR_STOP = frozenset({"error", "interrupted", "interruption", "cancellation", "canceled"})


class CaptureIncomplete(ValueError):
    """Native source is not a complete observation; do not publish a seal."""


@dataclass
class NativeCapture:
    source: str
    dialect: str
    harness: str
    session_id: str
    kind: str
    path: Path | None
    scope: str = _SCOPE
    status: CaptureStatus = "complete"
    reason: str | None = None
    observed_from: str = field(default_factory=utc_now_iso)
    _sha256: Any = field(default_factory=hashlib.sha256)
    _count: int = 0
    _pending_tools: int = 0
    _pending_tool_ids: set[str] = field(default_factory=lambda: set())
    _last_assistant_stop: str | None = None
    _last_assistant_data: dict[str, object] | None = None
    _last_error: bool = False
    _open_tasks: int = 0
    _aborted: bool = False
    _lineage: bool = False

    def fail(self, status: CaptureStatus, reason: str) -> None:
        if self.status == "complete":
            self.status = status
            self.reason = reason

    def _record(self, raw: str) -> SnapshotRecord:
        record = SnapshotRecord(source=self.source, ordinal=self._count, raw=raw)
        self._sha256.update(raw.encode("utf-8"))
        self._count += 1
        return record

    def records(self) -> Iterator[SnapshotRecord]:
        if self.status != "complete":
            return
        if self.kind == "opencode_db":
            yield from self._opencode_records()
            return
        if self.path is None:
            raise FileNotFoundError(f"Session file for '{self.session_id}' not found")
        yield from self._jsonl_records(self.path)

    def finish(self) -> SnapshotObservation:
        if self.status != "complete":
            raise CaptureIncomplete(self.reason or self.status)
        return SnapshotObservation(
            observed_until=utc_now_iso(),
            sources=(
                SourceRevision(
                    source=self.source,
                    sha256=self._sha256.hexdigest(),
                    records=self._count,
                ),
            ),
        )

    def _jsonl_records(self, path: Path) -> Iterator[SnapshotRecord]:
        before = _revision(path)
        with path.open("rb") as handle:
            while True:
                raw = read_jsonl_frame(handle)
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    raise ValueError("Incomplete JSONL frame")
                stripped = raw.strip()
                if not stripped:
                    continue
                reject_unframed_storage_frame(stripped)
                text = raw.decode("utf-8")
                try:
                    payload = json.loads(stripped.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError("Malformed JSONL record") from exc
                if isinstance(payload, dict):
                    self._observe(cast("dict[str, object]", payload))
                yield self._record(text)
        if _revision(path) != before:
            self.fail("unavailable", "Native source changed during capture")
            return
        self._finish_tail()

    def _opencode_records(self) -> Iterator[SnapshotRecord]:
        for event in iter_opencode_db_events(session_id=self.session_id, db_path=self.path):
            self._observe_opencode(event)
            raw = (
                json.dumps(
                    event,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
            yield self._record(raw)
        self._finish_tail()

    def _observe(self, event: dict[str, object]) -> None:
        if self.harness == "pi":
            self._observe_pi(event)
        elif self.harness == "claude":
            self._observe_claude(event)
        elif self.harness == "codex":
            self._observe_codex(event)

    def _observe_pi(self, event: dict[str, object]) -> None:
        if (
            event.get("type") == "session"
            and isinstance(event.get("id"), str)
            and event["id"] != self.session_id
        ):
            raise ValueError("Native Pi session identity does not match the selected session")
        message = event.get("message")
        if event.get("type") not in {"message", "message_end"} or not isinstance(message, dict):
            return
        payload = cast("dict[str, object]", message)
        role = str(payload.get("role", "")).strip().lower()
        if role == "assistant":
            stop = str(payload.get("stopReason", "")).strip().lower()
            if stop:
                self._last_assistant_stop = stop
            content = payload.get("content")
            if isinstance(content, list):
                for item in cast("list[object]", content):
                    if isinstance(item, dict) and item.get("type") == "toolCall":
                        self._pending_tools += 1
        elif role in {"toolresult", "tool_result"}:
            self._pending_tools = max(0, self._pending_tools - 1)

    def _observe_claude(self, event: dict[str, object]) -> None:
        event_type = str(event.get("type") or event.get("event_type") or "").strip().lower()
        stop = str(event.get("stop_reason") or event.get("stopReason") or "").strip().lower()
        subtype = str(event.get("subtype") or "").strip().lower()
        error = event.get("is_error") is True or event_type in {"error", "interrupt"}
        if error or stop in _CLAUDE_ERROR_STOP or subtype in {"error", "unauthorized"}:
            self._last_error = True
        else:
            self._last_error = False
        _walk_tool_balance(event, self._pending_tool_ids)

    def _observe_codex(self, event: dict[str, object]) -> None:
        if _contains_key(event, "history_base"):
            self._lineage = True
        kind = _codex_kind(event)
        if kind == "task_started":
            self._open_tasks += 1
        elif kind == "task_complete":
            self._open_tasks = max(0, self._open_tasks - 1)
            self._aborted = False
        elif kind == "turn_aborted":
            self._aborted = True
        elif kind in {"assistant", "agent_message"}:
            self._aborted = False

    def _observe_opencode(self, event: dict[str, object]) -> None:
        if event.get("table") == "message":
            payload = _require_json_payload(event.get("row"))
            if str(payload.get("role", "")).strip().lower() == "assistant":
                self._last_assistant_data = payload
            for part in cast("list[object]", event.get("parts") or ()):
                if isinstance(part, dict):
                    part_payload = _require_json_payload(part)
                    _observe_opencode_part(part_payload, self)
        elif event.get("table") == "part":
            payload = _require_json_payload(event.get("row"))
            _observe_opencode_part(payload, self)

    def _finish_tail(self) -> None:
        if self.status != "complete":
            return
        if self._lineage:
            self.fail(
                "unsupported",
                "Codex paginated history_base lineage is unsupported for capture",
            )
            return
        if self.harness == "pi" and (
            self._last_assistant_stop in PI_INCOMPLETE_STOP_REASONS or self._pending_tools
        ):
            self.fail("known-incomplete", "Native capture is known incomplete: unfinished Pi tail")
            return
        if (
            self.harness == "opencode"
            and self._last_assistant_data is not None
            and not _opencode_assistant_completed(self._last_assistant_data)
        ):
            self.fail(
                "known-incomplete",
                "Native capture is known incomplete: unfinished OpenCode response",
            )
            return
        if self.harness == "claude" and (self._pending_tool_ids or self._last_error):
            self.fail(
                "known-incomplete",
                "Native capture is known incomplete: unfinished Claude tail",
            )
            return
        if self.harness == "codex" and (self._open_tasks or self._aborted):
            self.fail(
                "known-incomplete",
                "Native capture is known incomplete: unfinished Codex tail",
            )


def native_capture(
    *,
    kind: str,
    harness: str | None,
    session_id: str,
    path: Path | None,
) -> NativeCapture:
    normalized = (harness or "").strip().lower()
    dialect = _DIALECT.get(normalized)
    capture = NativeCapture(
        source=session_id,
        dialect=dialect or "unsupported",
        harness=normalized,
        session_id=session_id,
        kind=kind,
        path=path,
    )
    if dialect is None:
        capture.fail(
            "unsupported",
            f"Native capture is unsupported for harness {normalized!r}",
        )
    elif kind not in {"native_file", "opencode_db"}:
        capture.fail("unsupported", f"Native capture is unsupported for source kind {kind!r}")
    elif kind == "native_file" and path is None:
        raise FileNotFoundError(f"Session file for '{session_id}' not found")
    return capture


def _revision(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _require_json_payload(row: object) -> dict[str, object]:
    if not isinstance(row, dict):
        raise ValueError("Malformed OpenCode payload")
    data = row.get("data")
    if data is None:
        return {}
    if isinstance(data, dict):
        return cast("dict[str, object]", data)
    if not isinstance(data, str):
        raise ValueError("Malformed OpenCode payload")
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError("Malformed OpenCode payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("Malformed OpenCode payload")
    return cast("dict[str, object]", payload)


def _observe_opencode_part(payload: dict[str, object], capture: NativeCapture) -> None:
    kind = payload.get("type")
    state = payload.get("state")
    if kind == "tool" and isinstance(state, dict):
        status = str(cast("dict[str, object]", state).get("status", "")).strip().lower()
        if status in _OPENCODE_PENDING:
            capture.fail(
                "known-incomplete",
                "Native capture is known incomplete: unfinished OpenCode tool",
            )


def _opencode_assistant_completed(data: dict[str, object]) -> bool:
    """OpenCode marks a finished assistant response with ``time.completed``."""
    time_obj = data.get("time")
    if not isinstance(time_obj, dict):
        return False
    return cast("dict[str, object]", time_obj).get("completed") is not None


def _walk_tool_balance(value: object, pending: set[str]) -> None:
    if isinstance(value, dict):
        payload = cast("dict[str, object]", value)
        block_type = str(payload.get("type", "")).strip().lower()
        if block_type == "tool_use":
            ident = payload.get("id")
            if isinstance(ident, str) and ident:
                pending.add(ident)
        elif block_type == "tool_result":
            ident = payload.get("tool_use_id") or payload.get("toolUseId")
            if isinstance(ident, str):
                pending.discard(ident)
        for item in payload.values():
            _walk_tool_balance(item, pending)
    elif isinstance(value, list):
        for item in cast("list[object]", value):
            _walk_tool_balance(item, pending)


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        payload = cast("dict[str, object]", value)
        if key in payload:
            return True
        return any(_contains_key(item, key) for item in payload.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in cast("list[object]", value))
    return False


def _codex_kind(event: dict[str, object]) -> str:
    payload = event.get("payload")
    nested = ""
    if isinstance(payload, dict):
        nested = str(cast("dict[str, object]", payload).get("type") or "").strip().lower()
    top = str(event.get("type") or "").strip().lower()
    if top == "response_item" and nested == "message":
        role = ""
        if isinstance(payload, dict):
            role = str(cast("dict[str, object]", payload).get("role") or "").strip().lower()
        if role == "assistant":
            return "assistant"
    if nested in {"task_started", "task_complete", "turn_aborted"}:
        return nested
    if top in {"task_started", "task_complete", "turn_aborted"}:
        return top
    return nested or top
