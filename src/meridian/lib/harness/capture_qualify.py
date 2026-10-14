"""Per-harness observers that qualify native capture tails."""

from __future__ import annotations

from typing import Protocol

from meridian.lib.harness.semantics import PI_INCOMPLETE_STOP_REASONS
from meridian.lib.utils.json_shapes import json_list, json_object, loads_json_object

_OPENCODE_PENDING = frozenset({"pending", "running"})
_OPENCODE_V2_DIALECT = "opencode.transcript.v2"
_OPENCODE_V2_TERMINAL_OUTCOMES = frozenset({"succeeded", "failed", "interrupted"})
_CLAUDE_ERROR_STOP = frozenset({"error", "interrupted", "interruption", "cancellation", "canceled"})


class CaptureObserver(Protocol):
    def observe(self, event: dict[str, object]) -> None: ...

    def incomplete_reason(self) -> str | None: ...


class PiObserver:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._pending_tools = 0
        self._last_assistant_stop: str | None = None

    def observe(self, event: dict[str, object]) -> None:
        if (
            event.get("type") == "session"
            and isinstance(event.get("id"), str)
            and event["id"] != self._session_id
        ):
            raise ValueError("Native Pi session identity does not match the selected session")
        message = event.get("message")
        payload = json_object(message)
        if event.get("type") not in {"message", "message_end"} or payload is None:
            return
        role = str(payload.get("role", "")).strip().lower()
        if role == "assistant":
            stop = str(payload.get("stopReason", "")).strip().lower()
            if stop:
                self._last_assistant_stop = stop
            content = payload.get("content")
            if items := json_list(content):
                for item in items:
                    part = json_object(item)
                    if part is not None and part.get("type") == "toolCall":
                        self._pending_tools += 1
        elif role in {"toolresult", "tool_result"}:
            self._pending_tools = max(0, self._pending_tools - 1)

    def incomplete_reason(self) -> str | None:
        if self._last_assistant_stop in PI_INCOMPLETE_STOP_REASONS or self._pending_tools:
            return "Native capture is known incomplete: unfinished Pi tail"
        return None


class ClaudeObserver:
    def __init__(self) -> None:
        self._pending_tool_ids: set[str] = set()
        self._last_error = False

    def observe(self, event: dict[str, object]) -> None:
        event_type = str(event.get("type") or event.get("event_type") or "").strip().lower()
        stop = str(event.get("stop_reason") or event.get("stopReason") or "").strip().lower()
        subtype = str(event.get("subtype") or "").strip().lower()
        error = event.get("is_error") is True or event_type in {"error", "interrupt"}
        if error or stop in _CLAUDE_ERROR_STOP or subtype in {"error", "unauthorized"}:
            self._last_error = True
        else:
            self._last_error = False
        _walk_tool_balance(event, self._pending_tool_ids)

    def incomplete_reason(self) -> str | None:
        if self._pending_tool_ids or self._last_error:
            return "Native capture is known incomplete: unfinished Claude tail"
        return None


class CodexObserver:
    def __init__(self) -> None:
        self._open_tasks = 0
        self._aborted = False
        self._lineage = False

    def observe(self, event: dict[str, object]) -> None:
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

    def incomplete_reason(self) -> str | None:
        if self._lineage:
            return "Codex paginated history_base lineage is unsupported for capture"
        if self._open_tasks or self._aborted:
            return "Native capture is known incomplete: unfinished Codex tail"
        return None


class OpenCodeObserver:
    def __init__(self) -> None:
        self._pending_tools = 0
        self._last_assistant_data: dict[str, object] | None = None

    def observe(self, event: dict[str, object]) -> None:
        if event.get("table") == "message":
            payload = _require_json_payload(event.get("row"))
            if str(payload.get("role", "")).strip().lower() == "assistant":
                self._last_assistant_data = payload
            for part in json_list(event.get("parts")) or []:
                if json_object(part) is not None:
                    self._observe_part(_require_json_payload(part))
        elif event.get("table") == "part":
            self._observe_part(_require_json_payload(event.get("row")))

    def incomplete_reason(self) -> str | None:
        if self._pending_tools:
            return "Native capture is known incomplete: unfinished OpenCode tool"
        if self._last_assistant_data is not None and not _opencode_assistant_completed(
            self._last_assistant_data
        ):
            return "Native capture is known incomplete: unfinished OpenCode response"
        return None

    def _observe_part(self, payload: dict[str, object]) -> None:
        kind = payload.get("type")
        state = json_object(payload.get("state"))
        if kind == "tool" and state is not None:
            status = str(state.get("status", "")).strip().lower()
            if status in _OPENCODE_PENDING:
                self._pending_tools += 1


class OpenCodeV2Observer:
    """Qualify the V2 ``session_message`` dialect.

    V2 turn completion is not the V1 ``message.time.completed`` marker: the
    native session writes an ``idle_outcome`` on ``session_v2`` and an ``idle``
    ``session_message`` carrying ``outcome``. Assistant tool parts live in the
    message's ``content`` list. An unfinished tail — a pending/running tool, or
    no terminal outcome — must not seal.
    """

    def __init__(self) -> None:
        self._pending_tools = 0
        self._session_outcome: str | None = None
        self._idle_outcome: str | None = None

    def observe(self, event: dict[str, object]) -> None:
        if event.get("version") != 2:
            return
        data = json_object(event.get("data"))
        if data is None:
            return
        kind = str(event.get("type", "")).strip().lower()
        if kind == "session":
            outcome = data.get("idle_outcome")
            if isinstance(outcome, str) and outcome.strip():
                self._session_outcome = outcome.strip().lower()
        elif kind == "idle":
            outcome = data.get("outcome")
            if isinstance(outcome, str) and outcome.strip():
                self._idle_outcome = outcome.strip().lower()
        elif kind == "assistant":
            self._observe_tools(data.get("content"))

    def incomplete_reason(self) -> str | None:
        if self._pending_tools:
            return "Native capture is known incomplete: unfinished OpenCode tool"
        if self._completion_outcome() is None:
            return "Native capture is known incomplete: unfinished OpenCode response"
        return None

    def _observe_tools(self, content: object) -> None:
        items = json_list(content)
        if items is None:
            return
        for item in items:
            part = json_object(item)
            if part is None:
                continue
            if str(part.get("type", "")).strip().lower() != "tool":
                continue
            state = json_object(part.get("state"))
            if state is None:
                continue
            status = str(state.get("status", "")).strip().lower()
            if status in _OPENCODE_PENDING:
                self._pending_tools += 1

    def _completion_outcome(self) -> str | None:
        outcome = self._idle_outcome or self._session_outcome
        if outcome in _OPENCODE_V2_TERMINAL_OUTCOMES:
            return outcome
        return None


class _IdleObserver:
    def observe(self, event: dict[str, object]) -> None:
        return

    def incomplete_reason(self) -> str | None:
        return None


def observer_for(
    harness: str, session_id: str, dialect: str | None = None
) -> CaptureObserver:
    if harness == "pi":
        return PiObserver(session_id)
    if harness == "claude":
        return ClaudeObserver()
    if harness == "codex":
        return CodexObserver()
    if harness == "opencode":
        if dialect == _OPENCODE_V2_DIALECT:
            return OpenCodeV2Observer()
        return OpenCodeObserver()
    return _IdleObserver()


def _require_json_payload(row: object) -> dict[str, object]:
    payload = json_object(row)
    if payload is None:
        raise ValueError("Malformed OpenCode payload")
    data = payload.get("data")
    if data is None:
        return {}
    nested = json_object(data)
    if nested is not None:
        return nested
    if not isinstance(data, str):
        raise ValueError("Malformed OpenCode payload")
    decoded = loads_json_object(data)
    if decoded is None:
        raise ValueError("Malformed OpenCode payload")
    return decoded


def _opencode_assistant_completed(data: dict[str, object]) -> bool:
    time_obj = json_object(data.get("time"))
    if time_obj is None:
        return False
    return time_obj.get("completed") is not None


def _walk_tool_balance(value: object, pending: set[str]) -> None:
    payload = json_object(value)
    if payload is not None:
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
    elif items := json_list(value):
        for item in items:
            _walk_tool_balance(item, pending)


def _contains_key(value: object, key: str) -> bool:
    payload = json_object(value)
    if payload is not None:
        if key in payload:
            return True
        return any(_contains_key(item, key) for item in payload.values())
    if items := json_list(value):
        return any(_contains_key(item, key) for item in items)
    return False


def _codex_kind(event: dict[str, object]) -> str:
    payload = json_object(event.get("payload"))
    nested = ""
    if payload is not None:
        nested = str(payload.get("type") or "").strip().lower()
    top = str(event.get("type") or "").strip().lower()
    if top == "response_item" and nested == "message":
        role = ""
        if payload is not None:
            role = str(payload.get("role") or "").strip().lower()
        if role == "assistant":
            return "assistant"
    if nested in {"task_started", "task_complete", "turn_aborted"}:
        return nested
    if top in {"task_started", "task_complete", "turn_aborted"}:
        return top
    return nested or top
