"""Legacy Pi recovery binds only proven 0.6.7 sessions: real marker, journals, archives."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.ops import legacy_native_import as legacy
from meridian.lib.state import session_store
from meridian.lib.state.retention_archive import append_receipt, capture_record, publish_archive
from meridian.lib.state.spawn.model import SpawnRecord, TerminalFacts
from meridian.lib.state.spawn.repository import record_to_stored_state

START, STOP = "2026-07-17T12:43:00Z", "2026-07-17T12:50:00Z"
HEADER_TIME = "2026-07-17T12:43:21.116Z"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MERIDIAN_HOME", str(home / ".meridian"))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    root, cwd = tmp_path / "runtime", tmp_path / "project"
    root.mkdir()
    cwd.mkdir()
    return home / ".meridian/meridian-pi/sessions", root, cwd


def _chat(root: Path, chat: str, cwd: Path, *, kind: str = "spawn", **facts: object) -> None:
    event = {
        "event": "start",
        "chat_id": chat,
        "harness": "pi",
        "harness_session_id": "",
        "kind": kind,
        "model": "test",
        "started_at": START,
        "control_root": str(cwd),
        "execution_cwd": str(cwd / "worktree"),
        "session_instance_id": f"g-{chat}",
        **facts,
    }
    generation = f"g-{chat}"
    stop = {"event": "stop", "chat_id": chat, "stopped_at": STOP, "session_instance_id": generation}
    with (root / "sessions.jsonl").open("a") as handle:
        handle.write(json.dumps(event) + "\n" + json.dumps(stop) + "\n")


def _spawn(
    root: Path,
    spawn: str,
    chat: str,
    cwd: Path,
    *,
    prompt: str | None = None,
    report: str | None = None,
    session_dir: Path | None = None,
) -> SpawnRecord:
    record = SpawnRecord(
        id=spawn,
        chat_id=chat,
        harness="pi",
        history_id=uuid4(),
        started_at=START,
        control_root=str(cwd),
        execution_cwd=str(cwd / "worktree"),
        status="succeeded",
        terminal=TerminalFacts(exit_code=0, finished_at=STOP, published_at=STOP, origin="runner"),
    )
    directory = root / "spawns" / spawn
    directory.mkdir(parents=True)
    stored = record_to_stored_state(record)
    if prompt is not None:
        (directory / "starting-prompt.md").write_text(prompt)
        stored = stored.model_copy(update={"prompt_length": len(prompt)})
    (directory / "state.json").write_text(stored.model_dump_json())
    if report is not None:
        (directory / "report.md").write_text(report)
    if session_dir is not None:
        (directory / "pi_runtime_meta.json").write_text(
            json.dumps({"schema_version": 1, "session_dir": str(session_dir)})
        )
    return record


def _journal(
    directory: Path,
    cwd: Path,
    *,
    user: str = "Reply OK and run no commands",
    assistant: str = "OK",
    version: int = 3,
    timestamp: str = HEADER_TIME,
) -> str:
    session_id = str(uuid4())
    directory.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "type": "session",
            "version": version,
            "id": session_id,
            "timestamp": timestamp,
            "cwd": str(cwd),
        },
        {
            "type": "message",
            "id": "a",
            "parentId": None,
            "message": {"role": "user", "content": [{"type": "text", "text": user}]},
        },
        {
            "type": "message",
            "id": "b",
            "parentId": "a",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant}],
                "provider": "p",
                "model": "m",
            },
        },
    ]
    stamp = timestamp.replace(":", "-").replace(".", "-")
    path = directory / f"{stamp}_{session_id}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return session_id


def _old_marker(root: Path) -> None:
    """The marker 0.6.7-era imports left: no pi_recovery_tried field."""
    report = legacy.import_legacy_native_sessions(root)
    assert report is not None
    marker = json.loads((root / legacy.MARKER).read_text())
    marker.pop("pi_recovery_tried")
    (root / legacy.MARKER).write_text(json.dumps(marker))


def _key(root: Path, chat: str) -> tuple[str | None, str | None]:
    record = session_store.get_session_record(root, chat)
    assert record is not None
    return record.harness_session_id, record.native_store


def test_matching_prompt_binds_once_and_marks_the_marker(env: tuple[Path, Path, Path]) -> None:
    pi_root, root, cwd = env
    _chat(root, "c1", cwd, spawn_id="p1")
    _spawn(root, "p1", "c1", cwd, prompt="Reply OK   and run\nno commands")
    # 0.6.7 ran Pi in the control root, not the recorded worktree.
    session_id = _journal(pi_root / "p1", cwd, version=1)
    _old_marker(root)

    result = legacy.recover_legacy_pi_sessions(root)

    assert result == legacy.PiRecovery(bound=1, unbound=())
    assert _key(root, "c1") == (session_id, str((pi_root / "p1").resolve()))
    marker = json.loads((root / legacy.MARKER).read_text())
    assert marker["pi_recovery_tried"] == ["c1"]
    assert "c1" not in marker["unbound"]["no_session_id"]
    journal = (root / "sessions.jsonl").read_bytes()
    assert b'"source":"legacy_pi_recovery"' in journal
    # One-shot: the marker now records the pass, so nothing is rescanned.
    assert legacy.recover_legacy_pi_sessions(root) == legacy.PiRecovery()
    assert (root / "sessions.jsonl").read_bytes() == journal


def test_prompt_mismatch_is_not_bound(env: tuple[Path, Path, Path]) -> None:
    # c8145's shape: the only candidate, right cwd and time, different first message.
    pi_root, root, cwd = env
    _chat(root, "c1", cwd, spawn_id="p1")
    _spawn(root, "p1", "c1", cwd, prompt="Summarize the billing review", report="# Report\n\nOK")
    _journal(pi_root / "p1", cwd)
    _old_marker(root)

    assert legacy.recover_legacy_pi_sessions(root) == legacy.PiRecovery(unbound=("c1",))
    assert _key(root, "c1") == (None, None)


def test_report_proves_when_no_prompt_was_retained(env: tuple[Path, Path, Path]) -> None:
    pi_root, root, cwd = env
    _chat(root, "c1", cwd)
    _spawn(root, "p1", "c1", cwd, report="# Report\n\nAll   done.\n")
    session_id = _journal(pi_root / "p1", cwd, assistant="All done.")
    _old_marker(root)

    assert legacy.recover_legacy_pi_sessions(root).bound == 1
    assert _key(root, "c1")[0] == session_id


def test_nothing_retained_is_not_bound(env: tuple[Path, Path, Path]) -> None:
    pi_root, root, cwd = env
    _chat(root, "c1", cwd, spawn_id="p1")
    _journal(pi_root / "p1", cwd)
    _old_marker(root)

    assert legacy.recover_legacy_pi_sessions(root) == legacy.PiRecovery(unbound=("c1",))


def test_two_candidates_are_not_bound(env: tuple[Path, Path, Path]) -> None:
    pi_root, root, cwd = env
    _chat(root, "c1", cwd, spawn_id="p1")
    _spawn(root, "p1", "c1", cwd, prompt="Reply OK and run no commands")
    _journal(pi_root / "p1", cwd)
    _journal(pi_root / "p1", cwd, timestamp="2026-07-17T12:44:00.000Z")
    _old_marker(root)

    assert legacy.recover_legacy_pi_sessions(root) == legacy.PiRecovery(unbound=("c1",))


def test_shared_root_primary_is_never_bound_automatically(env: tuple[Path, Path, Path]) -> None:
    pi_root, root, cwd = env
    _chat(root, "c1", cwd, kind="primary", spawn_id="p1")
    _spawn(root, "p1", "c1", cwd, prompt="Reply OK and run no commands")
    _journal(pi_root, cwd)
    # A spawned chat whose recorded session_dir is the shared root is refused too.
    _chat(root, "c2", cwd, spawn_id="p2")
    _spawn(root, "p2", "c2", cwd, prompt="Second prompt", session_dir=pi_root)
    _journal(pi_root, cwd, user="Second prompt", timestamp="2026-07-17T12:45:00.000Z")
    _old_marker(root)

    assert legacy.recover_legacy_pi_sessions(root) == legacy.PiRecovery(unbound=("c1", "c2"))
    assert _key(root, "c1") == _key(root, "c2") == (None, None)


def test_reclaimed_spawn_maps_and_proves_through_the_archive_catalog(
    env: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    pi_root, root, cwd = env
    # Retention reclaimed p1: no spawn dir, and the chat row never recorded spawn_id.
    _chat(root, "c1", cwd)
    record = _spawn(
        root, "p1", "c1", cwd, prompt="Reply OK and run no commands", session_dir=pi_root / "p1"
    )
    directory = root / "spawns" / "p1"
    (directory / "history.jsonl").write_text(json.dumps({"type": "runner"}) + "\n")
    archived = capture_record(directory, record, None, STOP)
    receipt = publish_archive(root, tmp_path / "archives", (archived,))
    append_receipt(root, receipt.model_copy(update={"event": "reclaimed"}))
    shutil.rmtree(directory)
    session_id = _journal(pi_root / "p1", cwd)
    _old_marker(root)

    assert legacy.recover_legacy_pi_sessions(root).bound == 1
    assert _key(root, "c1")[0] == session_id
