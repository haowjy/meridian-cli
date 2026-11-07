"""Legacy import's data-integrity boundary: real journals, native stores and locks."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.harness.claude import project_slug
from meridian.lib.ops import legacy_native_import as legacy
from meridian.lib.state import session_store
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import record_to_stored_state
from tests.support.opencode_db import write_opencode_db_session


def _chat(root: Path, number: int, harness: str, sid: str | None, **facts: object) -> None:
    root.mkdir(exist_ok=True)
    event = dict(
        event="start",
        chat_id=f"c{number}",
        harness=harness,
        harness_session_id=sid,
        model="test",
        started_at="2026-09-25T00:00:00Z",
        execution_cwd=str(root),
        **facts,
    )
    with (root / "sessions.jsonl").open("a") as handle:
        handle.write(json.dumps(event) + "\n")


def _spawn(root: Path, number: int, harness: str, sid: str) -> None:
    record = SpawnRecord(
        id=f"p{number}",
        chat_id=f"c{number}",
        harness=harness,
        harness_session_id=sid,
        execution_cwd=str(root),
    )
    path = root / "spawns" / record.id / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(record_to_stored_state(record).model_dump_json())


def _claude(home: Path, cwd: Path, sid: str, header: str | None = None) -> Path:
    path = home / ".claude" / "projects" / project_slug(cwd) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sessionId": header or sid})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "native hello"}]},
            }
        )
        + "\n"
    )
    return path


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MERIDIAN_HOME", str(home / ".meridian"))
    # Ambient harness config must never influence old records.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "wrong-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "wrong-codex"))
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "wrong.db"))
    return home, tmp_path / "runtime"


def test_import_exact_harness_stores_and_spawn_only_id(homes: tuple[Path, Path]) -> None:
    home, root = homes
    claude, codex, pi = str(uuid4()), str(uuid4()), str(uuid4())
    _chat(root, 1, "claude", None)
    _spawn(root, 1, "claude", claude)
    source = _claude(home, root, claude)
    _chat(root, 2, "codex", codex)
    rollout = home / ".codex/sessions/2026/09" / f"rollout-2026-09-25T00-00-00-{codex}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": codex}}) + "\n")
    _chat(root, 3, "pi", pi)
    _spawn(root, 3, "pi", pi)
    pi_store = home / ".meridian/meridian-pi/sessions/p3"
    pi_store.mkdir(parents=True)
    (pi_store / f"2026_{pi}.jsonl").write_text(json.dumps({"type": "session", "id": pi}) + "\n")
    _chat(root, 4, "opencode", "ses_import")
    database = home / ".local/share/opencode/opencode.db"
    write_opencode_db_session(db_path=database, session_id="ses_import", messages=[])
    _chat(root, 5, "cursor", "cursor-id")
    before = (root / "sessions.jsonl").read_bytes()
    report = legacy.report_legacy_native_import(root)
    assert (root / "sessions.jsonl").read_bytes() == before
    assert not (root / legacy.MARKER).exists()
    assert len(report.bindings) == 4
    imported = legacy.import_legacy_native_sessions(root)
    assert imported is not None and imported.counts == report.counts
    assert imported.unbound == {"unsupported": ["c5"]}
    assert imported.bindings["c1"] == (claude, str(source.parent))
    assert imported.bindings["c2"] == (codex, str(home / ".codex/sessions"))
    assert imported.bindings["c3"] == (pi, str(pi_store))
    assert imported.bindings["c4"] == ("ses_import", str(database))
    events = [json.loads(line) for line in (root / "sessions.jsonl").read_text().splitlines()]
    assert all(event["source"] == "legacy_import" for event in events[5:])
    after = (root / "sessions.jsonl").read_bytes()
    assert legacy.import_legacy_native_sessions(root) is None
    assert (root / "sessions.jsonl").read_bytes() == after
    for chat in session_store.list_all_session_records(root)[:4]:
        assert chat.native_store == imported.bindings[chat.chat_id][1]


def test_refuse_ambiguity_wrong_header_and_disagreeing_ids(homes: tuple[Path, Path]) -> None:
    home, root = homes
    other = root.parent / "other"
    for i in range(1, 6):
        _chat(root, i, "claude", f"id-{i}" if i != 5 else None, control_root=str(other))
    _claude(home, root, "id-1")
    _claude(home, other, "id-1")
    _claude(home, root, "id-2", header="wrong")
    _claude(home, root, "id-3")
    with (root / "sessions.jsonl").open("a") as handle:
        handle.write(
            json.dumps(dict(event="update", chat_id="c3", harness_session_ids=["id-3", "other-id"]))
            + "\n"
        )
    _spawn(root, 4, "claude", "disagrees")
    report = legacy.import_legacy_native_sessions(root)
    assert report is not None
    assert report.unbound == {
        "ambiguous": ["c1"],
        "missing": ["c2"],
        "ambiguous_id": ["c3", "c4"],
        "no_session_id": ["c5"],
    }
    assert all(chat.native_store is None for chat in session_store.list_all_session_records(root))


def test_crash_before_marker_retries_without_duplicate_events(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, root = homes
    _chat(root, 1, "claude", "native-id")
    _claude(home, root, "native-id")
    write = legacy.atomic_write_text

    def crash(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("crashed before marker")

    monkeypatch.setattr(legacy, "atomic_write_text", crash)
    with pytest.raises(RuntimeError, match="crashed"):
        legacy.import_legacy_native_sessions(root)
    assert not (root / legacy.MARKER).exists()
    events = (root / "sessions.jsonl").read_bytes()
    monkeypatch.setattr(legacy, "atomic_write_text", write)
    legacy.import_legacy_native_sessions(root)
    assert (root / "sessions.jsonl").read_bytes() == events
    assert (root / legacy.MARKER).is_file()


def _waiting_import(root: Path, started: object, finished: object) -> None:
    started.set()  # type: ignore[attr-defined]
    legacy.import_legacy_native_sessions(root)
    finished.set()  # type: ignore[attr-defined]


def test_unreadable_opencode_session_table_defers_without_marker(
    homes: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sqlite3

    home, root = homes
    _chat(root, 1, "opencode", "ses_corrupt")
    db = home / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE session_v2 (not_an_id TEXT)")
    legacy.maybe_import_legacy_native_sessions(root)
    assert "no such column: id" in capsys.readouterr().err
    assert not (root / legacy.MARKER).exists()


def test_torn_batch_recovers_only_uncommitted_bindings(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.state import session_binding

    home, root = homes
    for number in (1, 2):
        _chat(root, number, "claude", f"native-{number}")
        _claude(home, root, f"native-{number}")
    append = session_binding.append_chat_rows

    def torn_append(root, batches):
        first, second = [event for rows in batches.values() for event in rows]
        append(root, {first.chat_id: [first]})
        tail = second.model_dump_json(exclude_none=True)
        with (root / "sessions.jsonl").open("ab") as handle:
            handle.write(tail[: len(tail) // 2].encode())
        raise OSError("interrupted append")

    monkeypatch.setattr(session_binding, "append_chat_rows", torn_append)
    with pytest.raises(OSError, match="interrupted"):
        legacy.import_legacy_native_sessions(root)
    assert not (root / legacy.MARKER).exists()
    monkeypatch.setattr(session_binding, "append_chat_rows", append)
    report = legacy.import_legacy_native_sessions(root)
    assert report is not None and report.counts["claude"]["imported"] == 2
    events = [json.loads(line) for line in (root / "sessions.jsonl").read_text().splitlines()]
    assert [event["chat_id"] for event in events if event.get("source") == "legacy_import"] == [
        "c1",
        "c2",
    ]
