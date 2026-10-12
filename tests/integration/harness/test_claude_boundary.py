"""Cheap journal writer/decoder checks; launch tests cover live ownership."""

import io
import json
import os
import runpy
import sys
from pathlib import Path

from meridian.lib.harness.claude_boundary import read_observations
from meridian.lib.harness.claude_runtime_resolver import resolve_claude_runtime


def _record(monkeypatch, path: Path, **payload):
    runtime = resolve_claude_runtime(plugin_names=("meridian-session-boundary",))
    with monkeypatch.context() as env, io.TextIOWrapper(
        io.BytesIO(json.dumps(payload).encode()),
    ) as stdin:
        env.setenv("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH", str(path))
        env.setenv("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_NONCE", "nonce")
        env.setattr(sys, "stdin", stdin)
        runpy.run_path(str(Path(runtime.plugin_dirs[0]) / "hooks/record.py"), run_name="__main__")


def test_journal_preserves_order_and_private_bounded_records(tmp_path, monkeypatch, capsys):
    path = tmp_path / "observations.jsonl"
    for source, session_id in (
        ("startup", "entry"), ("clear", "next"), ("resume", "entry"), ("compact", "entry"),
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
        "entry", "next", "entry", "entry",
    ]
    assert [item.entry_operation for item in batch.observations] == ["create", None, "resume", None]
    assert batch.next_cursor == path.stat().st_size
    assert [item.key.session_id for item in read_observations(
        path, nonce="nonce", pid=os.getppid(), cursor=batch.observations[0].next_cursor,
    ).observations] == ["next", "entry", "entry"]
    _record(monkeypatch, path, hook_event_name="SessionStart", source="startup",
            session_id="oversized", transcript_path=str(tmp_path / "oversized.jsonl"),
            prompt="x" * (1024 * 1024))
    assert path.stat().st_size == batch.next_cursor
    assert read_observations(
        path, nonce="nonce", pid=os.getppid(), cursor=batch.next_cursor
    ).observations == ()


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
    _record(monkeypatch, path, hook_event_name="SessionStart", source="clear",
            session_id="next", transcript_path=str(tmp_path / "next.jsonl"))
    assert [item.key.session_id for item in read_observations(
        path, nonce="nonce", pid=os.getppid(), cursor=batch.next_cursor,
    ).observations] == ["next"]


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
