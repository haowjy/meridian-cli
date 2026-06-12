"""Reusable OpenCode SQLite transcript fixtures for tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

OpenCodeDbMessage = tuple[str, dict[str, object], list[dict[str, object]]]


def write_opencode_db_session(
    *,
    db_path: Path,
    session_id: str,
    messages: list[tuple[str, str]],
) -> None:
    write_opencode_db_session_with_parts(
        db_path=db_path,
        session_id=session_id,
        messages=[
            (role, {}, [{"type": "text", "text": text}]) for role, text in messages
        ],
    )


def write_opencode_db_session_with_parts(
    *,
    db_path: Path,
    session_id: str,
    messages: list[OpenCodeDbMessage],
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL,
                time_updated INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            """
        )
        now = 1_778_945_817_030
        connection.execute(
            "INSERT INTO session (id, time_created, time_updated) VALUES (?, ?, ?)",
            (session_id, now, now),
        )
        for message_index, (role, message_data, parts) in enumerate(messages):
            timestamp = now + (message_index * 100)
            message_id = f"msg_{message_index}"
            payload = {"role": role, "time": {"created": timestamp}, **message_data}
            connection.execute(
                "INSERT INTO message "
                "(id, session_id, time_created, time_updated, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (message_id, session_id, timestamp, timestamp, json.dumps(payload)),
            )
            for part_index, part in enumerate(parts):
                part_timestamp = timestamp + part_index + 1
                connection.execute(
                    "INSERT INTO part "
                    "(id, message_id, session_id, time_created, time_updated, data) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"prt_{message_index}_{part_index}",
                        message_id,
                        session_id,
                        part_timestamp,
                        part_timestamp,
                        json.dumps(part),
                    ),
                )
        connection.commit()
