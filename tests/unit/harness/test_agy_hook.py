"""Pins what ``agy`` reads from the hook and how queued messages are confirmed."""

import json
import os
from pathlib import Path

import pytest

from meridian.lib.harness.agy import inbox
from meridian.lib.harness.agy.hook import SESSION_CONTEXT, run_hook
from meridian.lib.harness.agy.mcp_server import format_result, scrubbed_env
from meridian.lib.harness.agy.state import process_dir, read_json, request_path


@pytest.fixture(autouse=True)
def agy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    return tmp_path


def _payload(conversation: str, store: Path, **extra: object) -> str:
    transcript = store / "brain" / conversation / ".system_generated" / "logs" / "t.jsonl"
    return json.dumps(
        {
            "conversationId": conversation,
            "artifactDirectoryPath": str(store / "brain" / conversation),
            "transcriptPath": str(transcript),
            **extra,
        }
    )


def test_session_start_injects_one_system_message(agy_home: Path) -> None:
    reply = run_hook("SessionStart", os.getpid(), _payload("c1", agy_home))

    assert reply is not None
    assert json.loads(reply) == {
        "injectSteps": [{"systemMessage": {"systemMessage": SESSION_CONTEXT}}]
    }


def test_no_opinion_prints_nothing_and_bad_input_never_raises(agy_home: Path) -> None:
    assert run_hook("PreInvocation", os.getpid(), _payload("c1", agy_home)) is None
    assert run_hook("PreInvocation", os.getpid(), "not json") is None
    assert run_hook("Stop", os.getpid(), _payload("c1", agy_home)) is None


def test_subagent_is_marked_and_ignored(agy_home: Path) -> None:
    parent_subagents = agy_home / "brain" / "par" / ".system_generated" / "subagents"
    parent_subagents.mkdir(parents=True)
    (parent_subagents / "kid.json").write_text("{}")
    pid = os.getpid()
    run_hook("SessionStart", pid, _payload("par", agy_home))
    inbox.enqueue("kid", "p1", "hello")

    assert run_hook("SessionStart", pid, _payload("kid", agy_home)) is None
    assert run_hook("PreInvocation", pid, _payload("kid", agy_home)) is None
    proc = process_dir(pid)
    assert proc is not None
    kid = read_json(request_path(proc, "kid"))
    par = read_json(request_path(proc, "par"))
    assert kid is not None and kid["role"] == "subagent"
    assert par is not None and par["role"] == "primary"


def test_delivered_batch_is_confirmed_by_transcript_and_lost_batch_is_resent(
    agy_home: Path,
) -> None:
    pid = os.getpid()
    payload = _payload("c1", agy_home)
    transcript = Path(json.loads(payload)["transcriptPath"])
    entry = inbox.enqueue("c1", "p7", "ping")

    first = run_hook("PreInvocation", pid, payload)
    assert first is not None
    steps = json.loads(first)["injectSteps"]
    assert [s["systemMessage"]["systemMessage"] for s in steps] == [
        f"Message from p7 [meridian:{entry.id}]\nping"
    ]

    # Not in the transcript yet: the next call hands it back.
    assert run_hook("PreInvocation", pid, payload) is not None

    transcript.parent.mkdir(parents=True)
    transcript.write_text(f"injected by a PreInvocation hook: [meridian:{entry.id}] ping\n")
    assert run_hook("PreInvocation", pid, payload) is None
    assert not inbox.inflight_path("c1").exists()


def test_unconfirmed_entry_is_dropped_after_max_attempts(agy_home: Path) -> None:
    pid = os.getpid()
    payload = _payload("c1", agy_home)
    inbox.enqueue("c1", "p7", "ping")

    replies = [run_hook("PreInvocation", pid, payload) for _ in range(inbox.MAX_ATTEMPTS + 1)]

    assert [reply is not None for reply in replies] == [True] * inbox.MAX_ATTEMPTS + [False]


def test_claim_gives_up_while_another_process_holds_the_lock(agy_home: Path) -> None:
    from meridian.lib.platform import fcntl

    inbox.enqueue("c1", "p7", "ping")
    lock_path = inbox._lock_path("c1")  # pyright: ignore[reportPrivateUsage]
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert inbox.claim_batch("c1", None) == []
    finally:
        os.close(fd)
    assert len(inbox.claim_batch("c1", None)) == 1


def test_mcp_child_env_drops_identity_but_keeps_home() -> None:
    env = scrubbed_env(
        {
            "MERIDIAN_SPAWN_ID": "p9",
            "_MERIDIAN_DEPTH": "2",
            "MERIDIAN_HOME": "/x",
            "PATH": "/bin",
        }
    )

    assert env == {"MERIDIAN_HOME": "/x", "PATH": "/bin"}


def test_tool_result_shows_stdout_stderr_and_exit_code() -> None:
    assert format_result("out\n", "", 0) == "out\n[exit code 0]"
    assert format_result("", "bad\n", 2) == "[stderr]\nbad\n[exit code 2]"
