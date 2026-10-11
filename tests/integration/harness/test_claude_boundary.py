"""Claude receipts must be owned, final, bounded and exact before native reads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import PostExit
from meridian.lib.harness.claude_boundary import read_boundary
from meridian.lib.harness.claude_runtime_resolver import resolve_claude_runtime


@pytest.mark.parametrize(
    "damage",
    ["missing", "truncated", "oversized", "version", "bool-version", "nonce", "pid", "bool-pid",
     "no-start", "relative", "wrong-basename", "path-id", "extra", "reason", "switch",
     "still-running"],
)
def test_invalid_or_nonfinal_receipt_cannot_verify_exit(tmp_path, damage):
    identity = {"session_id": "native-id", "transcript_path": str(tmp_path / "native-id.jsonl")}
    record = {
        "v": 1, "launch_nonce": "nonce", "pid": 123, "initial": identity.copy(),
        "current": identity, "event": "SessionEnd", "reason": "prompt_input_exit",
    }
    if damage in {"version", "bool-version"}:
        record["v"] = True if damage == "bool-version" else 2
    elif damage == "nonce":
        record["launch_nonce"] = "old"
    elif damage in {"pid", "bool-pid"}:
        record["pid"] = True if damage == "bool-pid" else 124
    elif damage == "no-start":
        record["initial"] = None
    elif damage == "relative":
        identity["transcript_path"] = "native-id.jsonl"
    elif damage == "wrong-basename":
        identity["transcript_path"] = str(tmp_path / "other.jsonl")
    elif damage == "path-id":
        identity["session_id"] = "../native-id"
    elif damage == "extra":
        record["unexpected"] = "value"
    elif damage == "reason":
        record["reason"] = "unknown"
    elif damage == "switch":
        record["reason"] = "clear"
    elif damage == "still-running":
        record["event"] = "SessionStart"
    path = tmp_path / "receipt.json"
    if damage != "missing":
        path.write_text("{" if damage == "truncated" else " " * 17000 if damage == "oversized"
                        else json.dumps(record))
    assert read_boundary(path, nonce="nonce", pid=123).exit is None


def test_owned_final_receipt_reports_initial_and_final_keys(tmp_path):
    initial = {"session_id": "first", "transcript_path": str(tmp_path / "first.jsonl")}
    current = {"session_id": "last", "transcript_path": str(tmp_path / "last.jsonl")}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({
        "v": 1, "launch_nonce": "nonce", "pid": 123, "initial": initial,
        "current": current, "event": "SessionEnd", "reason": "other",
    }))
    outcome = read_boundary(path, nonce="nonce", pid=123)
    assert outcome.entry_observed is not None and outcome.entry_observed.session_id == "first"
    assert outcome.exit is not None and outcome.exit.session_id == "last"
    assert read_boundary(path, nonce="nonce", pid=None) == PostExit()


@pytest.mark.parametrize(
    ("source", "previous"),
    [("clear", None), ("compact", None), ("startup", "{"), ("resume", "[]")],
)
def test_lost_start_or_corrupt_receipt_cannot_reseed_initial_identity(tmp_path, source, previous):
    plugin = Path(resolve_claude_runtime(plugin_name="meridian-session-boundary").plugin_dirs[0])
    path = tmp_path / "receipt.json"
    if previous is not None:
        path.write_text(previous)
    env = {**os.environ, "_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH": str(path),
           "_MERIDIAN_CLAUDE_SESSION_BOUNDARY_NONCE": "nonce"}
    for event in ("SessionStart", "SessionEnd"):
        subprocess.run(
            [sys.executable, str(plugin / "hooks/record.py")], env=env,
            input=json.dumps({"hook_event_name": event, "session_id": "late",
                              "transcript_path": str(tmp_path / "late.jsonl"),
                              "source": source, "reason": "prompt_input_exit"}),
            text=True, capture_output=True, check=True,
        )
    assert json.loads(path.read_text())["initial"] is None
    assert read_boundary(path, nonce="nonce", pid=os.getpid()) == PostExit()


@pytest.mark.parametrize("owner", ["foreign-pid", "deleted-directory"])
def test_hook_cannot_replace_another_process_or_recreate_its_spawn(tmp_path, owner):
    plugin = Path(resolve_claude_runtime(plugin_name="meridian-session-boundary").plugin_dirs[0])
    path = tmp_path / "receipt.json"
    previous = json.dumps({"launch_nonce": "nonce", "pid": os.getpid() + 1})
    if owner == "foreign-pid":
        path.write_text(previous)
    else:
        path = tmp_path / "deleted-spawn" / "receipt.json"
    result = subprocess.run(
        [sys.executable, str(plugin / "hooks/record.py")],
        env={**os.environ, "_MERIDIAN_CLAUDE_SESSION_BOUNDARY_PATH": str(path),
             "_MERIDIAN_CLAUDE_SESSION_BOUNDARY_NONCE": "nonce"},
        input=json.dumps({"hook_event_name": "SessionStart", "session_id": "native-id",
                          "transcript_path": str(tmp_path / "native-id.jsonl"),
                          "source": "startup"}),
        text=True, capture_output=True, check=True,
    )
    assert result.stdout == result.stderr == ""
    if owner == "foreign-pid":
        assert path.read_text() == previous
    else:
        assert not path.parent.exists()
