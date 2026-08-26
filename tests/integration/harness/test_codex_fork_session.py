"""Unit tests for Codex adapter session forking via file copy + SQLite clone."""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path
from uuid import UUID

import pytest

from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.platform import IS_WINDOWS


def _setup_codex_state(
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_session_id: str,
    codex_home: Path | None = None,
) -> tuple[Path, Path]:
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())
    resolved_codex_home = codex_home if codex_home is not None else fake_home / ".codex"

    source_rollout_path = (
        resolved_codex_home
        / "sessions"
        / "2026"
        / "03"
        / "30"
        / f"rollout-2026-03-30T12-00-00-{source_session_id}.jsonl"
    )
    source_rollout_path.parent.mkdir(parents=True, exist_ok=True)
    source_rollout_path.write_text(
        "".join(
            (
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": source_session_id, "cwd": "/tmp/repo"},
                    }
                )
                + "\n",
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant", "text": "hello"},
                    }
                )
                + "\n",
            )
        ),
        encoding="utf-8",
    )

    db_path = resolved_codex_home / "state_5.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO threads (id, rollout_path, created_at, updated_at, source, title)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source_session_id,
            str(source_rollout_path),
            1710000000,
            1710000000,
            "exec",
            "source title",
        ),
    )
    connection.commit()
    connection.close()
    return db_path, source_rollout_path


def test_codex_fork_session_uses_codex_home_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_session_id = "77777777-7777-4777-8777-777777777777"
    codex_home_override = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home_override.as_posix())
    db_path, source_rollout_path = _setup_codex_state(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        source_session_id=source_session_id,
        codex_home=codex_home_override,
    )

    forked_session_id = CodexAdapter().fork_session(source_session_id)

    connection = sqlite3.connect(db_path)
    forked_row = connection.execute(
        "SELECT rollout_path FROM threads WHERE id = ?",
        (forked_session_id,),
    ).fetchone()
    connection.close()

    assert forked_row is not None
    forked_rollout_path = Path(forked_row[0])
    assert forked_rollout_path != source_rollout_path
    assert forked_rollout_path.is_file()
    assert codex_home_override in forked_rollout_path.parents


def test_codex_fork_session_copies_rollout_and_inserts_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_session_id = "11111111-1111-4111-8111-111111111111"
    db_path, source_rollout_path = _setup_codex_state(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        source_session_id=source_session_id,
    )
    adapter = CodexAdapter()
    source_rollout_path.chmod(0o600)

    assert adapter.capabilities.supports_session_fork is True

    forked_session_id = adapter.fork_session(source_session_id)
    assert forked_session_id != source_session_id
    assert str(UUID(forked_session_id)) == forked_session_id

    connection = sqlite3.connect(db_path)
    forked_row = connection.execute(
        "SELECT rollout_path, source, title FROM threads WHERE id = ?",
        (forked_session_id,),
    ).fetchone()
    total_rows = connection.execute("SELECT COUNT(*) FROM threads").fetchone()
    connection.close()

    assert forked_row is not None
    assert total_rows is not None and total_rows[0] == 2
    assert forked_row[1] == "exec"
    assert forked_row[2] == "source title"

    forked_rollout_path = Path(forked_row[0])
    assert forked_rollout_path != source_rollout_path
    assert forked_rollout_path.is_file()
    if not IS_WINDOWS:
        assert stat.S_IMODE(forked_rollout_path.stat().st_mode) == 0o600

    source_lines = source_rollout_path.read_text(encoding="utf-8").splitlines()
    forked_lines = forked_rollout_path.read_text(encoding="utf-8").splitlines()
    assert len(source_lines) == len(forked_lines) == 2
    assert json.loads(source_lines[0])["payload"]["id"] == source_session_id
    assert json.loads(forked_lines[0])["payload"]["id"] == forked_session_id
    assert json.loads(forked_lines[1]) == json.loads(source_lines[1])


def test_codex_fork_session_drops_partial_live_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_session_id = "11111111-1111-4111-8111-111111111111"
    db_path, source_rollout_path = _setup_codex_state(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        source_session_id=source_session_id,
    )
    with source_rollout_path.open("ab") as handle:
        handle.write(b'{"type":"response_item","payload":{"type":"message"')

    forked_session_id = CodexAdapter().fork_session(source_session_id)

    connection = sqlite3.connect(db_path)
    forked_row = connection.execute(
        "SELECT rollout_path FROM threads WHERE id = ?", (forked_session_id,)
    ).fetchone()
    connection.close()
    assert forked_row is not None
    forked_lines = Path(forked_row[0]).read_bytes().splitlines(keepends=True)
    assert len(forked_lines) == 2
    assert all(line.endswith(b"\n") for line in forked_lines)
    assert all(isinstance(json.loads(line), dict) for line in forked_lines)


def test_codex_fork_session_rejects_invalid_complete_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_session_id = "11111111-1111-4111-8111-111111111111"
    db_path, source_rollout_path = _setup_codex_state(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        source_session_id=source_session_id,
    )
    with source_rollout_path.open("ab") as handle:
        handle.write(b"not-json\n")

    with pytest.raises(RuntimeError, match="line 3 is not valid JSON"):
        CodexAdapter().fork_session(source_session_id)

    connection = sqlite3.connect(db_path)
    total_rows = connection.execute("SELECT COUNT(*) FROM threads").fetchone()
    connection.close()
    assert total_rows is not None and total_rows[0] == 1
    assert len(list(source_rollout_path.parent.glob("rollout-*.jsonl"))) == 1


def test_codex_fork_session_replace_failure_leaves_no_partial_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_session_id = "11111111-1111-4111-8111-111111111111"
    forked_session_id = "22222222-2222-4222-8222-222222222222"
    db_path, source_rollout_path = _setup_codex_state(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        source_session_id=source_session_id,
    )
    expected_target_path = source_rollout_path.with_name(
        source_rollout_path.name.replace(source_session_id, forked_session_id, 1)
    )
    monkeypatch.setattr("meridian.lib.harness.codex.uuid4", lambda: UUID(forked_session_id))

    def _replace_raises(source: Path, target: Path) -> None:
        _ = source, target
        raise OSError("replace boom")

    monkeypatch.setattr("meridian.lib.harness.codex.os.replace", _replace_raises)

    with pytest.raises(RuntimeError, match="Failed to fork Codex session: replace boom"):
        CodexAdapter().fork_session(source_session_id)

    assert not expected_target_path.exists()
    assert not list(source_rollout_path.parent.glob(f".{expected_target_path.name}.*.tmp"))

    connection = sqlite3.connect(db_path)
    total_rows = connection.execute("SELECT COUNT(*) FROM threads").fetchone()
    connection.close()
    assert total_rows is not None and total_rows[0] == 1


def test_codex_fork_session_removes_rollout_when_db_insert_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_session_id = "11111111-1111-4111-8111-111111111111"
    forked_session_id = "33333333-3333-4333-8333-333333333333"
    db_path, source_rollout_path = _setup_codex_state(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        source_session_id=source_session_id,
    )
    expected_target_path = source_rollout_path.with_name(
        source_rollout_path.name.replace(source_session_id, forked_session_id, 1)
    )
    monkeypatch.setattr("meridian.lib.harness.codex.uuid4", lambda: UUID(forked_session_id))

    connection = sqlite3.connect(db_path)
    connection.execute(
        f"""
        CREATE TRIGGER reject_fork_insert
        BEFORE INSERT ON threads
        WHEN NEW.id != '{source_session_id}'
        BEGIN
            SELECT RAISE(FAIL, 'insert blocked');
        END;
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="Failed to fork Codex session: insert blocked"):
        CodexAdapter().fork_session(source_session_id)

    assert not expected_target_path.exists()

    connection = sqlite3.connect(db_path)
    total_rows = connection.execute("SELECT COUNT(*) FROM threads").fetchone()
    connection.close()
    assert total_rows is not None and total_rows[0] == 1
