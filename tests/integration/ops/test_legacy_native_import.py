"""Legacy import's data-integrity boundary: real journals, native stores and locks."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.harness.claude import project_slug
from meridian.lib.ops import legacy_native_import as legacy
from meridian.lib.platform.locking import lock_file
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


def test_concurrent_import_waits_and_rechecks_marker(homes: tuple[Path, Path]) -> None:
    home, root = homes
    _chat(root, 1, "claude", "native-id")
    _claude(home, root, "native-id")
    ctx = multiprocessing.get_context("spawn")
    started, finished = ctx.Event(), ctx.Event()
    with lock_file(root / "locks/legacy-native-import.lock"):
        process = ctx.Process(target=_waiting_import, args=(root, started, finished))
        process.start()
        assert started.wait(10)
        assert not finished.wait(0.2)
        legacy.import_legacy_native_sessions(root)
    process.join(10)
    assert process.exitcode == 0
    assert len((root / "sessions.jsonl").read_text().splitlines()) == 2


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_help_does_not_import_and_runtime_read_does(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    from meridian.lib.ops.runtime import resolve_runtime_authority_for_read

    home, root = homes
    _chat(root, 1, "claude", "native-id")
    _claude(home, root, "native-id")
    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", str(root))
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    monkeypatch.delenv("_MERIDIAN_DEPTH", raising=False)
    result = subprocess.run(
        [sys.executable, "-m", "meridian", flag],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert not (root / legacy.MARKER).exists()
    resolve_runtime_authority_for_read(root)
    assert (root / legacy.MARKER).is_file()


def test_damaged_spawn_defers_import_without_blocking_runtime_resolution(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from meridian.lib.ops.runtime import resolve_runtime_authority_for_read

    home, root = homes
    _chat(root, 1, "claude", "native-id")
    _claude(home, root, "native-id")
    broken = root / "spawns/p1/state.json"
    broken.parent.mkdir(parents=True)
    broken.write_text('{"v": 3, "broken": true}')
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", str(root))
    assert resolve_runtime_authority_for_read(root).runtime_root == root
    assert "Native session import deferred" in capsys.readouterr().err
    assert not (root / legacy.MARKER).exists()
    assert session_store.get_session_record(root, "c1").native_store is None  # type: ignore[union-attr]


def test_null_old_id_array_and_crash_recovery_counts(homes: tuple[Path, Path]) -> None:
    home, root = homes
    _chat(root, 1, "claude", "native-id", harness_session_ids=None)
    _claude(home, root, "native-id")
    report = legacy.import_legacy_native_sessions(root)
    assert report is not None and report.counts["claude"]["imported"] == 1
    (root / legacy.MARKER).unlink()  # Simulate losing only the completion marker.
    recovered = legacy.import_legacy_native_sessions(root)
    assert recovered is not None and recovered.counts["claude"]["imported"] == 1
    assert len((root / "sessions.jsonl").read_text().splitlines()) == 2


@pytest.mark.parametrize("harness", ["claude", "codex", "pi", "opencode"])
def test_imported_chat_native_log_and_continue_projection(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    from meridian.cli.primary_launch import run_primary_launch
    from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
    from meridian.lib.core.types import HarnessId
    from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
    from tests.support.executables import prepend_fake_executables
    from tests.support.launch import stub_bundle_request_and_resolve

    home, root = homes
    sid = str(uuid4()) if harness != "opencode" else "ses_log"
    _chat(root, 1, harness, sid, spawn_id="p1", control_root=str(root))
    record = SpawnRecord(
        id="p1",
        chat_id="c1",
        harness=harness,
        harness_session_id=sid,
        execution_cwd=str(root),
        control_root=str(root),
        kind="primary",
        launch_policy_snapshot=LaunchPolicySnapshot(
            model="test", harness=harness, agent_opt_out=True
        ),
    )
    state = root / "spawns/p1/state.json"
    state.parent.mkdir(parents=True)
    state.write_text(record_to_stored_state(record).model_dump_json())
    (root / "mars.toml").write_text(f'[settings]\ntargets = [".{harness}"]\n')
    if harness == "claude":
        source = _claude(home, root, sid)
    elif harness == "codex":
        source = home / ".codex/sessions" / f"rollout-2026-09-25T00-00-00-{sid}.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": sid}})
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "native hello"}],
                    },
                }
            )
            + "\n"
        )
    elif harness == "pi":
        source = home / ".meridian/meridian-pi/sessions/p1" / f"2026_{sid}.jsonl"
        source.parent.mkdir(parents=True)
        source.write_text(
            json.dumps({"type": "session", "id": sid})
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "id": "msg1",
                    "parentId": None,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "native hello"}],
                    },
                }
            )
            + "\n"
        )
    else:
        source = home / ".local/share/opencode/opencode.db"
        write_opencode_db_session(
            db_path=source, session_id=sid, messages=[("assistant", "native hello")]
        )
    monkeypatch.setenv("_MERIDIAN_RUNTIME_DIR", str(root))
    monkeypatch.setenv("_MERIDIAN_DEPTH", "0")
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    log = session_log_sync(SessionLogInput(ref="c1", project_root=str(root)))
    assert log.session_id == sid
    assert any(message.content == "native hello" for message in log.messages)
    prepend_fake_executables(monkeypatch, root / "bin", harness)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="test",
        harness=HarnessId(harness),
        harness_model="test/test" if harness == "opencode" else "test",
    )
    output = run_primary_launch(
        project_root=root,
        continue_ref="c1",
        fork_ref=None,
        fork_fresh_ref=None,
        model=None,
        harness=None,
        agent=None,
        work="",
        task_dir=None,
        yolo=False,
        approval=None,
        autocompact=None,
        effort=None,
        sandbox=None,
        timeout=None,
        dry_run=True,
        passthrough=(),
        skills=(),
    )
    assert output.exit_code == 0
    assert sid in output.format_text() or str(source) in output.format_text()


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


def test_shape_invalid_legacy_rows_do_not_break_import(homes: tuple[Path, Path]) -> None:
    home, root = homes
    _chat(root, 1, "claude", "native-id", task_cwd=5)
    _claude(home, root, "native-id")
    with (root / "sessions.jsonl").open("a") as handle:
        handle.write('{"event":"historical_import","record":null}\n')
    # Normal replay drops the invalid start, so this journal has no eligible chats.
    report = legacy.import_legacy_native_sessions(root)
    assert report is not None and not report.bindings


def test_torn_batch_recovers_only_uncommitted_bindings(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.lib.state import session_binding

    home, root = homes
    for number in (1, 2):
        _chat(root, number, "claude", f"native-{number}")
        _claude(home, root, f"native-{number}")
    append = session_binding.append_durable_jsonl_line

    def torn_append(path: Path, lines: str) -> None:
        first, second = lines.splitlines(keepends=True)
        with path.open("ab") as handle:
            handle.write((first + second[: len(second) // 2]).encode())
        raise OSError("interrupted append")

    monkeypatch.setattr(session_binding, "append_durable_jsonl_line", torn_append)
    with pytest.raises(OSError, match="interrupted"):
        legacy.import_legacy_native_sessions(root)
    assert not (root / legacy.MARKER).exists()
    monkeypatch.setattr(session_binding, "append_durable_jsonl_line", append)
    report = legacy.import_legacy_native_sessions(root)
    assert report is not None and report.counts["claude"]["imported"] == 2
    events = [json.loads(line) for line in (root / "sessions.jsonl").read_text().splitlines()]
    assert [event["chat_id"] for event in events if event.get("source") == "legacy_import"] == [
        "c1",
        "c2",
    ]
