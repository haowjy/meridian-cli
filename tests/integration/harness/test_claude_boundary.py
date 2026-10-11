"""Cheap receipt/writer checks; process ownership is covered by the launch test."""

import io
import json
import os
import runpy
import sys
from pathlib import Path

import pytest

from meridian.lib.harness.claude_boundary import read_boundary
from meridian.lib.harness.claude_runtime_resolver import resolve_claude_runtime


@pytest.mark.parametrize("overrides", [
    {"launch_nonce": "stale"},
    {"pid": 124},
    {"initial": None},
    {"event": "SessionStart"},
    {"reason": "clear"},
    {"current": {"session_id": "native-id", "transcript_path": "/wrong.jsonl"}},
])
def test_invalid_or_nonfinal_receipt_cannot_verify_exit(tmp_path, overrides):
    identity = {"session_id": "native-id", "transcript_path": str(tmp_path / "native-id.jsonl")}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({
        "v": 1, "launch_nonce": "nonce", "pid": 123, "initial": identity,
        "current": identity, "event": "SessionEnd", "reason": "prompt_input_exit",
        **overrides,
    }))
    assert read_boundary(path, nonce="nonce", pid=123).exit is None


@pytest.mark.parametrize("raw", ["{", " " * 17000], ids=["torn", "oversized"])
def test_unreadable_receipt_is_unverified(tmp_path, raw):
    path = tmp_path / "receipt.json"
    path.write_text(raw)
    assert read_boundary(path, nonce="nonce", pid=123).exit is None


def _record(monkeypatch, path: Path, **payload):
    runtime = resolve_claude_runtime(plugin_names=("meridian-session-boundary",))
    with monkeypatch.context() as env, io.TextIOWrapper(
        io.BytesIO(json.dumps(payload).encode()),
    ) as stdin:
        env.setenv("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH", str(path))
        env.setenv("_MERIDIAN_CLAUDE_SESSION_BOUNDARY_NONCE", "nonce")
        env.setattr(sys, "stdin", stdin)
        runpy.run_path(str(Path(runtime.plugin_dirs[0]) / "hooks/record.py"), run_name="__main__")


@pytest.mark.parametrize(("source", "previous"), [("clear", None), ("fork", "{")])
def test_lost_start_or_corrupt_receipt_cannot_reseed_initial_identity(
    tmp_path, monkeypatch, source, previous,
):
    path = tmp_path / "receipt.json"
    if previous is not None:
        path.write_text(previous)
    _record(monkeypatch, path, hook_event_name="SessionStart", session_id="late", source=source,
            transcript_path=str(tmp_path / "late.jsonl"))
    assert json.loads(path.read_text())["initial"] is None


def test_hook_accepts_large_prompt_but_persists_only_private_identity(
    tmp_path, monkeypatch, capsys,
):
    path = tmp_path / "receipt.json"
    _record(monkeypatch, path, hook_event_name="SessionStart", session_id="native-id",
            transcript_path=str(tmp_path / "native-id.jsonl"), source="startup",
            prompt="private prompt" * 4000)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    saved = path.read_text()
    assert "private prompt" not in saved and len(saved) < 16 * 1024
    assert json.loads(saved)["initial"]["session_id"] == "native-id"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("owner", ["foreign-pid", "deleted-directory"])
def test_hook_cannot_replace_another_process_or_recreate_its_spawn(tmp_path, monkeypatch, owner):
    path = tmp_path / "receipt.json"
    previous = json.dumps({"launch_nonce": "nonce", "pid": os.getppid() + 1})
    if owner == "foreign-pid":
        path.write_text(previous)
    else:
        path = tmp_path / "deleted-spawn" / "receipt.json"
    _record(monkeypatch, path, hook_event_name="SessionStart", session_id="native-id",
            transcript_path=str(tmp_path / "native-id.jsonl"), source="startup")
    if owner == "foreign-pid":
        assert path.read_text() == previous
    else:
        assert not path.parent.exists()
