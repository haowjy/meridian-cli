"""Cheap journal writer/decoder checks; launch tests cover live ownership."""

import io
import json
import os
import runpy
import sys
import time
from pathlib import Path

from meridian.lib.harness.claude_boundary import (
    latest_messaging_socket,
    read_observations,
)
from meridian.lib.harness.claude_runtime_resolver import resolve_claude_runtime


def _record(monkeypatch, path: Path, **payload):
    runtime = resolve_claude_runtime(plugin_names=("meridian-session-boundary",))
    with (
        monkeypatch.context() as env,
        io.TextIOWrapper(
            io.BytesIO(json.dumps(payload).encode()),
        ) as stdin,
    ):
        env.setenv("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH", str(path))
        env.setenv("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_NONCE", "nonce")
        env.setattr(sys, "stdin", stdin)
        runpy.run_path(str(Path(runtime.plugin_dirs[0]) / "hooks/record.py"), run_name="__main__")


def test_journal_preserves_order_and_private_bounded_records(tmp_path, monkeypatch, capsys):
    path = tmp_path / "observations.jsonl"
    for source, session_id in (
        ("startup", "entry"),
        ("clear", "next"),
        ("resume", "entry"),
        ("compact", "entry"),
    ):
        _record(
            monkeypatch,
            path,
            hook_event_name="SessionStart",
            session_id=session_id,
            transcript_path=str(tmp_path / f"{session_id}.jsonl"),
            source=source,
            prompt="private prompt" * 4000,
        )

    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    saved = path.read_text()
    assert "private prompt" not in saved
    assert path.stat().st_mode & 0o777 == 0o600
    batch = read_observations(path, nonce="nonce", pid=os.getppid(), cursor=0)
    assert [item.key.session_id for item in batch.observations] == [
        "entry",
        "next",
        "entry",
        "entry",
    ]
    assert [item.entry_operation for item in batch.observations] == ["create", None, "resume", None]
    assert batch.next_cursor == path.stat().st_size
    assert [
        item.key.session_id
        for item in read_observations(
            path,
            nonce="nonce",
            pid=os.getppid(),
            cursor=batch.observations[0].next_cursor,
        ).observations
    ] == ["next", "entry", "entry"]
    _record(
        monkeypatch,
        path,
        hook_event_name="SessionStart",
        source="startup",
        session_id="oversized",
        transcript_path=str(tmp_path / "oversized.jsonl"),
        prompt="x" * (1024 * 1024),
    )
    assert path.stat().st_size == batch.next_cursor
    assert (
        read_observations(
            path, nonce="nonce", pid=os.getppid(), cursor=batch.next_cursor
        ).observations
        == ()
    )


def test_decoder_rejects_foreign_nonce_pid_and_torn_tail(tmp_path, monkeypatch):
    path = tmp_path / "observations.jsonl"
    _record(
        monkeypatch,
        path,
        hook_event_name="SessionStart",
        session_id="entry",
        transcript_path=str(tmp_path / "entry.jsonl"),
        source="startup",
    )
    with path.open("ab") as handle:
        handle.write(b'{"v":2')

    assert read_observations(path, nonce="foreign", pid=os.getppid(), cursor=0).observations == ()
    assert read_observations(path, nonce="nonce", pid=os.getppid() + 1, cursor=0).observations == ()
    batch = read_observations(path, nonce="nonce", pid=os.getppid(), cursor=0)
    assert [item.key.session_id for item in batch.observations] == ["entry"]
    assert batch.next_cursor < path.stat().st_size
    _record(
        monkeypatch,
        path,
        hook_event_name="SessionStart",
        source="clear",
        session_id="next",
        transcript_path=str(tmp_path / "next.jsonl"),
    )
    assert [
        item.key.session_id
        for item in read_observations(
            path,
            nonce="nonce",
            pid=os.getppid(),
            cursor=batch.next_cursor,
        ).observations
    ] == ["next"]


def test_writer_records_messaging_socket_without_token(tmp_path, monkeypatch):
    path = tmp_path / "observations.jsonl"
    inbox = tmp_path / "claude.sock"
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", str(inbox))
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "must-not-be-recorded")

    _record(
        monkeypatch,
        path,
        hook_event_name="SessionStart",
        session_id="entry",
        transcript_path=str(tmp_path / "entry.jsonl"),
        source="startup",
    )

    saved = path.read_text(encoding="utf-8")
    record = json.loads(next(line for line in saved.splitlines() if line))
    assert record["messaging_socket"] == str(inbox)
    assert record["recorded_at"] <= time.time()
    assert "must-not-be-recorded" not in saved
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in saved
    batch = read_observations(path, nonce="nonce", pid=os.getppid(), cursor=0)
    assert [item.key.session_id for item in batch.observations] == ["entry"]

    relative = tmp_path / "relative-socket.jsonl"
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "relative.sock")
    _record(
        monkeypatch,
        relative,
        hook_event_name="SessionStart",
        session_id="entry",
        transcript_path=str(tmp_path / "entry.jsonl"),
        source="startup",
    )
    unusable = json.loads(next(line for line in relative.read_text().splitlines() if line))
    assert "messaging_socket" not in unusable
    assert isinstance(unusable["recorded_at"], float)


def test_latest_messaging_socket_uses_last_matching_pid_record(tmp_path):
    path = tmp_path / "observations.jsonl"
    pid = os.getpid()

    def record(
        session_id: str,
        *,
        record_pid: int = pid,
        socket_path: Path | None = None,
        recorded_at: float | None = None,
        version: int = 2,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "v": version,
            "launch_nonce": "nonce",
            "pid": record_pid,
            "source": "startup",
            "session_id": session_id,
            "transcript_path": str(tmp_path / f"{session_id}.jsonl"),
        }
        if socket_path is not None:
            payload["messaging_socket"] = str(socket_path)
        if recorded_at is not None:
            payload["recorded_at"] = recorded_at
        return payload

    first = tmp_path / "first.sock"
    foreign = tmp_path / "foreign.sock"
    latest = tmp_path / "latest.sock"
    ignored_version = tmp_path / "ignored-version.sock"
    lines = [
        record("first", socket_path=first, recorded_at=1.0),
        record("foreign", record_pid=pid + 1, socket_path=foreign),
        record("without-socket"),
        record("latest", socket_path=latest, recorded_at=2.0),
        record("invalid-socket", socket_path=Path("relative.sock"), recorded_at=3.0),
        record("future", socket_path=ignored_version, version=3),
        {**record("wrong-type-socket"), "messaging_socket": 123, "recorded_at": "soon"},
    ]
    path.write_bytes(b"\n".join(json.dumps(line).encode() for line in lines) + b'\n{"v":2')

    assert latest_messaging_socket(path, pid=pid) == (latest, 2.0)
    assert latest_messaging_socket(path, pid=pid + 2) is None
    assert [
        item.key.session_id
        for item in read_observations(path, nonce="nonce", pid=pid, cursor=0).observations
    ] == ["first", "without-socket", "latest", "invalid-socket", "wrong-type-socket"]


def test_hook_does_not_recreate_deleted_spawn_parent(tmp_path, monkeypatch):
    path = tmp_path / "deleted-spawn" / "observations.jsonl"
    _record(
        monkeypatch,
        path,
        hook_event_name="SessionStart",
        session_id="entry",
        transcript_path=str(tmp_path / "entry.jsonl"),
        source="startup",
    )
    assert not path.parent.exists()
