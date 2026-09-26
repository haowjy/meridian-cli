"""`session repair`: read-only candidate listing, validated bind, and every refusal."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from meridian.lib.harness.claude import project_slug
from meridian.lib.ops.session_repair import SessionRepairInput, repair_session_reference_sync
from meridian.lib.state import session_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write

START, STOP = "2026-07-17T12:43:00Z", "2026-07-17T12:50:00Z"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MERIDIAN_HOME", str(home / ".meridian"))
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    project = tmp_path / "repo"
    project.mkdir()
    runtime = resolve_project_runtime_root_for_write(project)
    runtime.mkdir(parents=True, exist_ok=True)
    return home, project, runtime


def _chat(runtime: Path, chat: str, cwd: Path, *, harness: str = "pi") -> None:
    rows = [
        {
            "event": "start",
            "chat_id": chat,
            "harness": harness,
            "kind": "primary",
            "harness_session_id": None,
            "model": "test",
            "started_at": START,
            "execution_cwd": str(cwd),
            "session_instance_id": f"g-{chat}",
        },
        {"event": "stop", "chat_id": chat, "stopped_at": STOP, "session_instance_id": f"g-{chat}"},
    ]
    with (runtime / "sessions.jsonl").open("a") as handle:
        handle.writelines(json.dumps(row) + "\n" for row in rows)


def _pi(directory: Path, cwd: Path, *, timestamp: str = "2026-07-17T12:43:21.116Z") -> Path:
    session_id = str(uuid4())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{timestamp.replace(':', '-')}_{session_id}.jsonl"
    rows = [
        {
            "type": "session",
            "version": 2,
            "id": session_id,
            "timestamp": timestamp,
            "cwd": str(cwd),
        },
        {"type": "message", "message": {"role": "user", "content": "fix the flaky test"}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def _repair(project: Path, ref: str, native: Path | None = None, *, force: bool = False):
    return repair_session_reference_sync(
        SessionRepairInput(
            ref=ref,
            native=str(native) if native else None,
            force=force,
            project_root=str(project),
        )
    )


def test_inspect_lists_evidence_then_bind_validates_and_refuses(
    env: tuple[Path, Path, Path],
) -> None:
    home, project, runtime = env
    shared = home / ".meridian/meridian-pi/sessions"
    _chat(runtime, "c1", project)
    _chat(runtime, "c2", project)
    right = _pi(shared, project)
    elsewhere = _pi(shared, project / "other")
    late = _pi(shared, project, timestamp="2026-07-18T09:00:00.000Z")
    before = (runtime / "sessions.jsonl").read_bytes()

    listing = _repair(project, "c1")

    # Inspecting is read-only and ranks the full match first, with its exact command.
    assert (runtime / "sessions.jsonl").read_bytes() == before
    assert listing.action == "inspect"
    first = listing.candidates[0]
    assert (first.path, first.cwd_match, first.time_match, first.bound_to) == (
        str(right),
        True,
        True,
        None,
    )
    assert first.first_user == "fix the flaky test"
    assert first.bind_command == f"meridian session repair c1 --native {right}"
    others = {c.path: c for c in listing.candidates[1:]}
    assert set(others) == {str(elsewhere), str(late)}
    assert not others[str(elsewhere)].cwd_match and not others[str(late)].time_match
    assert all("--force" in (c.bind_command or "") for c in others.values())
    assert "time window yes" in listing.format_text()

    with pytest.raises(ValueError, match="cwd mismatch"):
        _repair(project, "c1", elsewhere)
    with pytest.raises(ValueError, match="outside the time window"):
        _repair(project, "c1", late)
    with pytest.raises(ValueError, match="not a valid pi session"):
        _repair(project, "c1", runtime / "sessions.jsonl")
    assert (runtime / "sessions.jsonl").read_bytes() == before

    bound = _repair(project, "c1", right)
    assert bound.action == "bound" and bound.forced == ()
    record = session_store.get_session_record(runtime, "c1")
    assert record is not None and record.native_key() is not None
    assert record.native_store == str(shared.resolve())
    assert b'"source":"user_repair"' in (runtime / "sessions.jsonl").read_bytes()

    assert _repair(project, "c1").action == "already_bound"
    with pytest.raises(ValueError, match=r"already bound .*immutable"):
        _repair(project, "c1", late, force=True)
    with pytest.raises(ValueError, match="already bound to c1"):
        _repair(project, "c2", right, force=True)
    assert _repair(project, "c2", late, force=True).forced


def test_bind_works_for_other_harnesses(env: tuple[Path, Path, Path]) -> None:
    home, project, runtime = env
    _chat(runtime, "c1", project, harness="claude")
    session_id = str(uuid4())
    store = home / ".claude/projects" / project_slug(project)
    store.mkdir(parents=True)
    path = store / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"sessionId": session_id, "cwd": str(project), "timestamp": START}) + "\n"
    )

    listing = _repair(project, "c1")
    assert [c.path for c in listing.candidates] == [str(path)]
    assert _repair(project, "c1", path).action == "bound"
    record = session_store.get_session_record(runtime, "c1")
    assert record is not None
    assert (record.harness_session_id, record.native_store) == (session_id, str(store))
