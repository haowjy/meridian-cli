"""OpenCode transcript provider with opencode.db preference."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import cast

from meridian.lib.harness.opencode_storage import resolve_opencode_storage_root


def resolve_opencode_db_path(launch_env: Mapping[str, str] | None = None) -> Path:
    """Resolve the OpenCode SQLite database path from the storage root."""

    return resolve_opencode_storage_root(launch_env).parent / "opencode.db"


def opencode_db_session_exists(
    *,
    session_id: str,
    db_path: Path | None = None,
) -> bool:
    """Return whether opencode.db contains a session row for ``session_id``."""

    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        return False
    resolved_db_path = db_path or resolve_opencode_db_path()
    if not resolved_db_path.is_file():
        return False

    try:
        with sqlite3.connect(
            f"file:{resolved_db_path}?mode=ro", uri=True, timeout=0.1
        ) as connection:
            row = connection.execute(
                "SELECT 1 FROM session WHERE id = ?",
                (normalized_session_id,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return False
    return row is not None


class OpenCodeStorageTranscriptProvider:
    """OpenCode storage provider that prefers opencode.db transcript rows."""

    def __init__(
        self,
        *,
        text_from_value: Callable[[object], str],
        iter_json_events: Callable[[Path], Iterator[dict[str, object]]],
    ) -> None:
        self._text_from_value = text_from_value
        self._iter_json_events = iter_json_events

    def supports(self, path: Path) -> bool:
        return (
            path.suffix == ".json"
            and path.parent.name in {"session_diff", "session"}
            and path.parent.parent.name == "storage"
        )

    def iter_events(self, path: Path) -> Iterator[dict[str, object]]:
        db_events = self._load_opencode_db_events(path)
        if db_events:
            yield from db_events
            return
        yield from self._iter_json_events(path)

    def _opencode_db_path_for_session_file(self, path: Path) -> Path | None:
        if path.parent.name not in {"session_diff", "session"}:
            return None
        storage_root = path.parent.parent
        if storage_root.name != "storage":
            return None
        return storage_root.parent / "opencode.db"

    def _load_opencode_db_events(self, path: Path) -> list[dict[str, object]]:
        session_id = path.stem.strip()
        if not session_id:
            return []

        db_path = self._opencode_db_path_for_session_file(path)
        if db_path is None:
            return []
        return list(iter_opencode_db_events(session_id=session_id, db_path=db_path))


def _load_json_object(value: object) -> dict[str, object] | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return cast("dict[str, object]", parsed)


def _text_from_mapping(mapping: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tool_body(state: dict[str, object]) -> str:
    raw_input = state.get("input")
    if not isinstance(raw_input, dict):
        return ""
    tool_input = cast("dict[str, object]", raw_input)
    direct = _text_from_mapping(
        tool_input,
        (
            "command",
            "filePath",
            "file_path",
            "path",
            "pattern",
            "description",
        ),
    )
    if direct:
        return direct
    if not tool_input:
        return ""
    try:
        return json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return str(tool_input)


def _tool_output(state: dict[str, object]) -> str:
    output = state.get("output")
    if isinstance(output, str) and output.strip():
        return output.strip()
    metadata = state.get("metadata")
    if isinstance(metadata, dict):
        metadata_output = cast("dict[str, object]", metadata).get("output")
        if isinstance(metadata_output, str) and metadata_output.strip():
            return metadata_output.strip()
    return ""


def _tool_events(part: dict[str, object]) -> list[dict[str, object]]:
    state = part.get("state")
    if not isinstance(state, dict):
        return []
    state_payload = cast("dict[str, object]", state)
    if str(state_payload.get("status", "")).strip().lower() != "completed":
        return []

    tool_name = str(part.get("tool", "tool")).strip() or "tool"
    body = _tool_body(state_payload)
    events: list[dict[str, object]] = [
        {
            "event_type": "response_item",
            "type": "function_call",
            "name": tool_name,
            "arguments": body,
        }
    ]
    output = _tool_output(state_payload)
    if output:
        events.append(
            {
                "event_type": "response_item",
                "type": "function_call_output",
                "output": output,
            }
        )
    return events


def _message_events(
    *,
    role: str,
    parts: list[dict[str, object]],
    text_from_value: Callable[[object], str],
) -> Iterator[dict[str, object]]:
    for part in parts:
        part_type = str(part.get("type", "")).strip().lower()
        if part_type == "text":
            text = text_from_value(part.get("text"))
            if text:
                yield {"role": role, "content": text}
            continue
        if part_type == "tool":
            yield from _tool_events(part)


def iter_opencode_db_events(
    *,
    session_id: str,
    db_path: Path | None = None,
    text_from_value: Callable[[object], str] | None = None,
) -> Iterator[dict[str, object]]:
    """Yield transcript events for one OpenCode DB session."""

    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        return

    resolved_db_path = db_path or resolve_opencode_db_path()
    if not resolved_db_path.is_file():
        return

    try:
        with sqlite3.connect(
            f"file:{resolved_db_path}?mode=ro", uri=True, timeout=0.1
        ) as connection:
            message_rows = connection.execute(
                """
                SELECT id, data, time_created
                FROM message
                WHERE session_id = ?
                ORDER BY time_created ASC, id ASC
                """,
                (normalized_session_id,),
            ).fetchall()
            part_rows = connection.execute(
                """
                SELECT message_id, data, time_created, id
                FROM part
                WHERE session_id = ?
                ORDER BY time_created ASC, id ASC
                """,
                (normalized_session_id,),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return

    parts_by_message: dict[str, list[dict[str, object]]] = defaultdict(list)
    for message_id, part_data, _time_created, _part_id in part_rows:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            continue
        part_obj = _load_json_object(part_data)
        if part_obj is not None:
            parts_by_message[normalized_message_id].append(part_obj)

    first_user_system_seen = False
    text_reader = text_from_value or _text_from_value
    for message_id, message_data, _time_created in message_rows:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            continue
        message_payload = _load_json_object(message_data)
        if message_payload is None:
            continue
        role = str(message_payload.get("role", "")).strip().lower()
        if role not in {"assistant", "user", "system"}:
            continue

        if role == "user" and not first_user_system_seen:
            first_user_system_seen = True
            system = text_reader(message_payload.get("system"))
            if system:
                yield {"opencode_db_setup": system}

        yield from _message_events(
            role=role,
            parts=parts_by_message.get(normalized_message_id, []),
            text_from_value=text_reader,
        )


def _text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _empty_json_events(_path: Path) -> Iterator[dict[str, object]]:
    return iter(())


def extract_last_assistant_report_from_session_path(path: Path) -> str | None:
    """Return the last assistant message text for one OpenCode session file."""

    provider = OpenCodeStorageTranscriptProvider(
        text_from_value=_text_from_value,
        iter_json_events=_empty_json_events,
    )
    last_assistant: str | None = None
    for event in provider.iter_events(path):
        if str(event.get("role", "")).strip().lower() != "assistant":
            continue
        content = _text_from_value(event.get("content"))
        if content:
            last_assistant = content
    return last_assistant


__all__ = [
    "OpenCodeStorageTranscriptProvider",
    "extract_last_assistant_report_from_session_path",
    "iter_opencode_db_events",
    "opencode_db_session_exists",
    "resolve_opencode_db_path",
]
