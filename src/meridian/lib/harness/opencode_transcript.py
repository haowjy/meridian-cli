"""OpenCode transcript provider with opencode.db preference."""

from __future__ import annotations

import base64
import json
import math
import sqlite3
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import closing
from itertools import groupby
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

    with closing(
        sqlite3.connect(resolved_db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    ) as connection:
        row = connection.execute(
            "SELECT 1 FROM session WHERE id = ?", (normalized_session_id,)
        ).fetchone()
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
        if database is not None and opencode_db_session_exists(
            session_id=path.stem, db_path=database
        ):
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
) -> tuple[list[dict[str, object]], str | None]:
    events: list[dict[str, object]] = []
    reason: str | None = None
    malformed = "Malformed OpenCode part content; rendering is incomplete."
    for part in parts:
        kind = part.get("type")
        if not isinstance(kind, str):
            reason = malformed
            continue
        if kind == "text":
            text = part.get("text")
            if not isinstance(text, str):
                reason = malformed
            elif text.strip():
                events.append({"role": role, "content": text.strip()})
        elif kind == "tool":
            state = part.get("state")
            name = part.get("tool")
            if not isinstance(state, dict) or not isinstance(name, str) or not name.strip():
                reason = malformed
                continue
            state = cast("dict[str, object]", state)
            if state.get("status") != "completed":
                reason = "Unrendered OpenCode tool state; rendering is incomplete."
                continue
            if not isinstance(state.get("input"), dict) or not isinstance(state.get("output"), str):
                reason = malformed
            events.extend(_tool_events(part))
        elif kind == "reasoning":
            if not isinstance(part.get("text"), str):
                reason = malformed
        elif kind not in {"step-start", "step-finish", "snapshot", "patch", "compaction"}:
            reason = "Unsupported OpenCode part content; rendering is incomplete."
    return events, reason


def _is_compaction_message(message_payload: dict[str, object]) -> bool:
    role = str(message_payload.get("role", "")).strip().lower()
    mode = str(message_payload.get("mode", "")).strip().lower()
    agent = str(message_payload.get("agent", "")).strip().lower()
    return role == "assistant" and (
        message_payload.get("summary") is True or (mode == "compaction" and agent == "compaction")
    )


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


def _raw_row(row: sqlite3.Row) -> dict[str, object]:
    """Preserve the three native tables' SQLite values in the raw-row dialect."""
    result: dict[str, object] = {}
    for key, value in zip(row.keys(), row, strict=True):
        if isinstance(value, bytes):
            result[key] = {"sqlite_type": "blob", "base64": base64.b64encode(value).decode("ascii")}
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Unsupported non-finite OpenCode column value")
        elif value is None or isinstance(value, (str, int, float)):
            result[key] = value
        else:
            raise ValueError("Unsupported OpenCode column value")
    return result


def iter_opencode_db_events(
    *,
    session_id: str,
    db_path: Path | None = None,
) -> Generator[dict[str, object]]:
    """Read the complete raw MessageV2 transcript scope in one read-only transaction.

    A session row is positive existence evidence, including valid-empty sessions.
    Each message carries its own raw part rows; unmatched session parts follow.
    No role, material type, payload string or native column is discarded here.
    This consistent read alone does not qualify unfinished responses for capture.
    """
    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        raise ValueError("OpenCode transcript requires an exact session ID")
    resolved_db_path = db_path or resolve_opencode_db_path()
    if not resolved_db_path.is_file():
        raise FileNotFoundError(resolved_db_path)
    with closing(
        sqlite3.connect(resolved_db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        for table, required in (
            ("session", {"id", "time_created", "time_updated"}),
            ("message", {"id", "session_id", "time_created", "time_updated", "data"}),
            ("part", {"id", "session_id", "message_id", "time_created", "time_updated", "data"}),
        ):
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not required <= columns:
                raise ValueError(f"Unsupported OpenCode {table} schema")
        session = connection.execute(
            "SELECT * FROM session WHERE id=?", (normalized_session_id,)
        ).fetchone()
        if session is None:
            raise ValueError("OpenCode transcript session does not exist")
        yield {
            "record": "opencode.transcript",
            "version": 1,
            "table": "session",
            "row": _raw_row(session),
        }
        # Traverse selected parts once. A per-message session+message predicate
        # can pick the native session-only index and rescan the session N times.
        parts = connection.execute(
            "SELECT p.* FROM part p CROSS JOIN message m "
            "WHERE p.session_id=? AND m.id=p.message_id AND m.session_id=p.session_id "
            "ORDER BY m.time_created,m.id,p.time_created,p.id",
            (normalized_session_id,),
        )
        groups = groupby(parts, key=lambda part: part["message_id"])
        pending = next(groups, None)
        for message in connection.execute(
            "SELECT * FROM message WHERE session_id=? ORDER BY time_created,id",
            (normalized_session_id,),
        ):
            message_parts: list[dict[str, object]] = []
            if pending is not None and pending[0] == message["id"]:
                message_parts = [_raw_row(part) for part in pending[1]]
                pending = next(groups, None)
            yield {
                "record": "opencode.transcript",
                "version": 1,
                "table": "message",
                "row": _raw_row(message),
                "parts": message_parts,
            }
        for part in connection.execute(
            "SELECT * FROM part p WHERE p.session_id=? AND NOT EXISTS "
            "(SELECT 1 FROM message m WHERE m.id=p.message_id AND m.session_id=p.session_id) "
            "ORDER BY p.time_created,p.id",
            (normalized_session_id,),
        ):
            yield {
                "record": "opencode.transcript",
                "version": 1,
                "table": "part",
                "row": _raw_row(part),
            }


def interpret_opencode_record(
    event: dict[str, object],
    *,
    include_user_setup: bool,
) -> tuple[list[dict[str, object]], bool, str | None]:
    """Translate a preserved row group once, at the shared normalization boundary.

    Return display events, whether this is a user message, and any rendering limit.
    Unknown/malformed material stays in raw authority, never successful empty display.
    """
    reason = "Malformed OpenCode transcript rows; rendering is incomplete."
    if event.get("version") != 1:
        return [], False, "Unsupported OpenCode transcript dialect; rendering is incomplete."
    row = event.get("row")
    if not isinstance(row, dict):
        return [], False, reason
    row = cast("dict[str, object]", row)
    table = event.get("table")
    if table == "session":
        return [], False, None
    if table != "message":
        return [], False, "Unassociated OpenCode transcript part; rendering is incomplete."
    message = _load_json_object(row.get("data"))
    raw_parts = event.get("parts")
    if message is None or not isinstance(raw_parts, list):
        return [], False, reason
    role = str(message.get("role", "")).strip().lower()
    if role not in {"assistant", "user", "system"}:
        return [], False, "Unsupported OpenCode message role; rendering is incomplete."
    parts: list[dict[str, object]] = []
    rendering_reason: str | None = None
    for raw_part in cast("list[object]", raw_parts):
        part = (
            _load_json_object(cast("dict[str, object]", raw_part).get("data"))
            if isinstance(raw_part, dict)
            else None
        )
        if part is None:
            rendering_reason = reason
            continue
        parts.append(part)
    material, material_reason = _message_events(role=role, parts=parts)
    rendering_reason = rendering_reason or material_reason
    if _is_compaction_message(message):
        return (
            [
                {"part": {"type": "compaction"}},
                _compaction_handoff_event(message_payload=message, parts=parts),
            ],
            False,
            rendering_reason,
        )
    events: list[dict[str, object]] = []
    if role == "user" and include_user_setup:
        system = _text_from_value(message.get("system"))
        if system:
            events.append({"opencode_db_setup": system})
    events.extend(material)
    return events, role == "user", rendering_reason


def _text_from_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def extract_last_assistant_report(events: Iterable[dict[str, object]]) -> str | None:
    """Read response text incrementally through the same raw-row interpretation."""
    from meridian.lib.harness.transcript import DefaultTranscriptEventParser, TranscriptNormalizer

    normalizer, parser = TranscriptNormalizer(), DefaultTranscriptEventParser()
    last_assistant: str | None = None
    for event in events:
        for message in normalizer.feed(event, parser).messages:
            if (
                message.role == "assistant"
                and message.tool_call is None
                and not message.is_tool_result
            ):
                last_assistant = message.content
    return last_assistant


def extract_last_assistant_report_from_session_path(path: Path) -> str | None:
    """Return the last assistant message text for one OpenCode session file."""
    provider = OpenCodeStorageTranscriptProvider(iter_json_events=lambda _path: iter(()))
    return extract_last_assistant_report(provider.iter_events(path))


__all__ = [
    "OpenCodeStorageTranscriptProvider",
    "extract_last_assistant_report",
    "extract_last_assistant_report_from_session_path",
    "iter_opencode_db_events",
    "opencode_db_session_exists",
    "resolve_opencode_db_path",
]
