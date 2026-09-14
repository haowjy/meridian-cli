"""OpenCode transcript provider with opencode.db preference."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import closing
from itertools import chain, groupby
from pathlib import Path
from typing import cast

from meridian.lib.harness.opencode_storage import resolve_opencode_home_dir


def resolve_opencode_db_path(launch_env: Mapping[str, str] | None = None) -> Path:
    """Resolve the OpenCode SQLite database path from the storage root."""

    return resolve_opencode_home_dir(launch_env) / "opencode.db"


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
        iter_json_events: Callable[[Path], Iterator[dict[str, object]]],
    ) -> None:
        self._iter_json_events = iter_json_events

    def supports(self, path: Path) -> bool:
        return (
            path.suffix == ".json"
            and path.parent.name in {"session_diff", "session"}
            and path.parent.parent.name == "storage"
        )

    def iter_events(self, path: Path) -> Iterator[dict[str, object]]:
        database = opencode_db_for_session_file(path)
        if database is not None:
            # Probe source usability without retaining a conversation-sized list.
            # The selected read itself is one SQLite snapshot.
            with closing(iter_opencode_db_events(session_id=path.stem, db_path=database)) as events:
                usable = any(_has_interaction_events([event]) for event in events)
            if usable:
                yield from iter_opencode_db_events(session_id=path.stem, db_path=database)
                return
        yield from self._iter_json_events(path)


def opencode_db_for_session_file(path: Path) -> Path | None:
    if path.parent.name not in {"session_diff", "session"}:
        return None
    storage_root = path.parent.parent
    return storage_root.parent / "opencode.db" if storage_root.name == "storage" else None


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


def _has_interaction_events(events: list[dict[str, object]]) -> bool:
    for event in events:
        role = str(event.get("role", "")).strip().lower()
        if role in {"assistant", "user"} and not _is_compaction_message(event):
            return True
        event_type = str(event.get("event_type", "")).strip().lower()
        item_type = str(event.get("type", "")).strip().lower()
        if event_type == "response_item" and item_type in {
            "function_call",
            "function_call_output",
        }:
            return True
    return False


def _is_compaction_message(message_payload: dict[str, object]) -> bool:
    role = str(message_payload.get("role", "")).strip().lower()
    mode = str(message_payload.get("mode", "")).strip().lower()
    agent = str(message_payload.get("agent", "")).strip().lower()
    return role == "assistant" and mode == "compaction" and agent == "compaction"


def _compaction_handoff_event(
    *,
    message_payload: dict[str, object],
    parts: list[dict[str, object]],
) -> dict[str, object]:
    event: dict[str, object] = {
        "role": "assistant",
        "mode": "compaction",
        "agent": "compaction",
    }
    if parts:
        event["parts"] = parts
    else:
        raw_parts = message_payload.get("parts")
        if isinstance(raw_parts, list):
            event["parts"] = raw_parts
        raw_part = message_payload.get("part")
        if isinstance(raw_part, dict):
            event["part"] = raw_part
    content = message_payload.get("content")
    if content is not None:
        event["content"] = content
    return event


def iter_opencode_db_events(
    *,
    session_id: str,
    db_path: Path | None = None,
    text_from_value: Callable[[object], str] | None = None,
) -> Generator[dict[str, object]]:
    """Yield transcript events for one OpenCode DB session."""

    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        return

    resolved_db_path = db_path or resolve_opencode_db_path()
    if not resolved_db_path.is_file():
        return

    first_user_system_seen = False
    text_reader = text_from_value or _text_from_value
    yielded = False
    try:
        with closing(
            sqlite3.connect(resolved_db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
        ) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT m.id,m.data,p.data FROM message m LEFT JOIN part p "
                "ON p.message_id=m.id AND p.session_id=m.session_id "
                "WHERE m.session_id=? ORDER BY m.time_created,m.id,p.time_created,p.id",
                (normalized_session_id,),
            )
            for _message_id, group in groupby(rows, key=lambda row: row[0]):
                first = next(group)
                message_payload = _load_json_object(first[1])
                if message_payload is None:
                    continue
                role = str(message_payload.get("role", "")).strip().lower()
                if role not in {"assistant", "user", "system"}:
                    continue
                message_parts = [
                    part
                    for row in chain((first,), group)
                    if (part := _load_json_object(row[2])) is not None
                ]
                yielded = True
                if _is_compaction_message(message_payload):
                    yield {"part": {"type": "compaction"}}
                    yield _compaction_handoff_event(
                        message_payload=message_payload, parts=message_parts
                    )
                    continue
                if role == "user" and not first_user_system_seen:
                    first_user_system_seen = True
                    system = text_reader(message_payload.get("system"))
                    if system:
                        yield {"opencode_db_setup": system}
                yield from _message_events(
                    role=role, parts=message_parts, text_from_value=text_reader
                )
    except (OSError, sqlite3.Error):
        if yielded:
            raise  # Do not report an interrupted snapshot as a complete transcript.
        return


def _text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _empty_json_events(_path: Path) -> Iterator[dict[str, object]]:
    return iter(())


def extract_last_assistant_report_from_session_path(path: Path) -> str | None:
    """Return the last assistant message text for one OpenCode session file."""

    provider = OpenCodeStorageTranscriptProvider(
        iter_json_events=_empty_json_events,
    )
    last_assistant: str | None = None
    for event in provider.iter_events(path):
        if _is_compaction_message(event):
            continue
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
