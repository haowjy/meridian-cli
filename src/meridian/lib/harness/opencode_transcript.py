"""OpenCode transcript provider with opencode.db preference."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast


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
        if db_path is None or not db_path.is_file():
            return []

        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.1) as connection:
                message_rows = connection.execute(
                    """
                    SELECT id, data, time_created
                    FROM message
                    WHERE session_id = ?
                    ORDER BY time_created ASC, id ASC
                    """,
                    (session_id,),
                ).fetchall()
                part_rows = connection.execute(
                    """
                    SELECT message_id, data, time_created, id
                    FROM part
                    WHERE session_id = ?
                    ORDER BY time_created ASC, id ASC
                    """,
                    (session_id,),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return []

        if not message_rows:
            return []

        parts_by_message: dict[str, list[dict[str, object]]] = defaultdict(list)
        for message_id, part_data, _time_created, _part_id in part_rows:
            normalized_message_id = str(message_id or "").strip()
            if not normalized_message_id:
                continue
            try:
                part_obj = json.loads(str(part_data))
            except (TypeError, ValueError):
                continue
            if isinstance(part_obj, dict):
                parts_by_message[normalized_message_id].append(cast("dict[str, object]", part_obj))

        events: list[dict[str, object]] = []
        for message_id, message_data, _time_created in message_rows:
            normalized_message_id = str(message_id or "").strip()
            if not normalized_message_id:
                continue
            try:
                message_obj = json.loads(str(message_data))
            except (TypeError, ValueError):
                continue
            if not isinstance(message_obj, dict):
                continue

            message_payload = cast("dict[str, object]", message_obj)
            role = str(message_payload.get("role", "")).strip().lower()
            if role not in {"assistant", "user", "system"}:
                continue

            text_parts: list[str] = []
            for part in parts_by_message.get(normalized_message_id, []):
                if str(part.get("type", "")).strip().lower() != "text":
                    continue
                text = self._text_from_value(part.get("text"))
                if text:
                    text_parts.append(text)

            content = "\n".join(text_parts).strip()
            if not content:
                content = self._text_from_value(message_payload.get("content"))
            if not content:
                content = self._text_from_value(message_payload.get("text"))
            if not content:
                continue
            events.append({"role": role, "content": content})

        return events


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
]
