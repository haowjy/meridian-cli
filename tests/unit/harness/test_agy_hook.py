"""Pins what ``agy`` reads from the hook and how queued messages are confirmed."""

import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from meridian.lib.core.delivery import MessageSender
from meridian.lib.harness.agy import inbox
from meridian.lib.harness.agy.holds import HeldRow, call_env
from meridian.lib.harness.agy.hook import run_hook
from meridian.lib.harness.agy.mcp_server import format_result, root_conversation
from meridian.lib.harness.agy.notices import send_owed_notices
from meridian.lib.harness.agy.state import (
    agy_root,
    asked_path,
    busy_path,
    inflight_path,
    process_dir,
    read_json,
    request_path,
    watch_lock_path,
    write_private_json,
)
from meridian.lib.platform import fcntl


@pytest.fixture(autouse=True)
def agy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "home"))
    return tmp_path


def _sender(spawn_id: str) -> MessageSender:
    return MessageSender(spawn_id, "/other/project")


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
    reply = run_hook("SessionStart", os.getpid(), _payload("c1", agy_home, cwd=str(agy_home)))

    assert reply is not None
    (step,) = json.loads(reply)["injectSteps"]
    text = step["systemMessage"]["systemMessage"]
    assert "# Meridian capabilities" in text
    assert "# Meridian spawn rules" in text


def _messages(reply: str | None) -> list[str]:
    assert reply is not None
    return [step["systemMessage"]["systemMessage"] for step in json.loads(reply)["injectSteps"]]


def test_static_guidance_and_session_facts_are_each_injected_once(agy_home: Path) -> None:
    pid = os.getpid()
    payload = _payload("c1", agy_home, cwd=str(agy_home))

    # The first call registers the conversation, whatever its event, and carries the guidance.
    (static,) = _messages(run_hook("PreInvocation", pid, payload))
    assert "# Meridian capabilities" in static
    assert run_hook("PreInvocation", pid, payload) is None

    proc = process_dir(pid)
    assert proc is not None
    row = {
        "spawn_id": "p1",
        "chat_id": "c1",
        "project_root": str(agy_home),
        "runtime_root": str(agy_home / "runtime"),
    }
    write_private_json(proc / "rows" / "c1.json", row)
    (facts,) = _messages(run_hook("PreInvocation", pid, payload))
    assert facts.startswith("# Meridian session")
    assert "MERIDIAN_" not in facts
    assert run_hook("PreInvocation", pid, payload) is None


def test_guidance_follows_the_model_context_across_resume_and_compaction(
    agy_home: Path,
) -> None:
    pid = os.getpid()
    payload = _payload("c1", agy_home, cwd=str(agy_home))
    transcript = Path(json.loads(payload)["transcriptPath"])
    transcript.parent.mkdir(parents=True)
    proc = process_dir(pid)
    assert proc is not None
    write_private_json(
        proc / "rows" / "c1.json",
        {
            "spawn_id": "p1",
            "chat_id": "c1",
            "project_root": str(agy_home),
            "runtime_root": str(agy_home / "runtime"),
        },
    )

    def step(kind: str, text: str = "") -> str:
        return json.dumps({"type": kind, "content": text}, separators=(",", ":")) + "\n"

    # A resumed conversation: this process registers it, and the transcript already holds both.
    held = _messages(run_hook("PreInvocation", pid, payload))
    transcript.write_text("".join(step("SYSTEM_MESSAGE", text) for text in held))
    for entry in (proc / "requests", proc / "briefed", proc / "seen"):
        for path in entry.glob("*"):
            path.unlink()
    assert run_hook("PreInvocation", pid, payload) is None

    # A compaction drops both; a half-written last line is not read yet.
    with transcript.open("a") as handle:
        handle.write(step("CHECKPOINT", "summary"))
        handle.write('{"type":"USER_INPUT","con')
    after = _messages(run_hook("PreInvocation", pid, payload))
    assert [text.startswith("# Meridian") for text in after] == [True, True]
    assert run_hook("PreInvocation", pid, payload) is None


def test_unwritable_markers_never_stop_the_inbox(agy_home: Path) -> None:
    pid = os.getpid()
    payload = _payload("c1", agy_home, cwd=str(agy_home))
    run_hook("PreInvocation", pid, payload)
    proc = process_dir(pid)
    assert proc is not None
    for marker in ("seen", "briefed"):
        for path in (proc / marker).glob("*"):
            path.unlink()
        if (proc / marker).exists():
            (proc / marker).rmdir()
        (proc / marker).write_text("not a directory")
    write_private_json(
        proc / "rows" / "c1.json",
        {"spawn_id": "p1", "chat_id": "c1", "project_root": str(agy_home), "runtime_root": "r"},
    )
    inbox.enqueue("c1", "hello", _sender("p9"))

    texts = _messages(run_hook("PreInvocation", pid, payload))
    assert any("hello" in text for text in texts)


def test_no_opinion_prints_nothing_and_bad_input_never_raises(agy_home: Path) -> None:
    assert run_hook("PreInvocation", os.getpid(), "not json") is None
    assert run_hook("Stop", os.getpid(), _payload("c1", agy_home)) is None


def test_subagent_is_marked_and_ignored(agy_home: Path) -> None:
    parent_subagents = agy_home / "brain" / "par" / ".system_generated" / "subagents"
    parent_subagents.mkdir(parents=True)
    (parent_subagents / "kid.json").write_text("{}")
    pid = os.getpid()
    run_hook("SessionStart", pid, _payload("par", agy_home))
    inbox.enqueue("kid", "hello", None)

    assert run_hook("SessionStart", pid, _payload("kid", agy_home)) is None
    assert run_hook("PreInvocation", pid, _payload("kid", agy_home)) is None
    proc = process_dir(pid)
    assert proc is not None
    kid = read_json(request_path(proc, "kid"))
    par = read_json(request_path(proc, "par"))
    assert kid is not None and kid["role"] == "subagent"
    assert par is not None and par["role"] == "primary"


def _stop_after_turn(agy_home: Path, conversation: str, **payload: object) -> str | None:
    proc = process_dir(os.getpid())
    assert proc is not None
    busy = busy_path(proc, conversation)
    busy.parent.mkdir(parents=True, exist_ok=True)
    busy.touch()  # what guard.sh does on every PreInvocation
    return run_hook("Stop", os.getpid(), _payload(conversation, agy_home, **payload))


def test_stop_asks_for_the_watcher_once_a_turn_then_ends_the_turn(agy_home: Path) -> None:
    pid = os.getpid()
    run_hook("SessionStart", pid, _payload("c1", agy_home))
    proc = process_dir(pid)
    assert proc is not None

    reply = json.loads(_stop_after_turn(agy_home, "c1") or "")

    assert reply["decision"] == "continue"
    assert f"meridian harness-watch agy --agy-pid {pid} --conversation c1" in reply["reason"]
    assert busy_path(proc, "c1").exists()  # the turn goes on
    assert asked_path(proc, "c1").exists()

    # The continuation's own Stop: declined or not, the turn ends and nothing asks again.
    assert _stop_after_turn(agy_home, "c1") is None
    assert not busy_path(proc, "c1").exists()
    assert not asked_path(proc, "c1").exists()


def test_stop_does_not_ask_for_a_running_watcher_a_cancelled_turn_or_a_subagent(
    agy_home: Path,
) -> None:
    pid = os.getpid()
    parent_subagents = agy_home / "brain" / "par" / ".system_generated" / "subagents"
    parent_subagents.mkdir(parents=True)
    (parent_subagents / "kid.json").write_text("{}")
    for conversation in ("par", "kid"):
        run_hook("SessionStart", pid, _payload(conversation, agy_home))
    proc = process_dir(pid)
    assert proc is not None

    assert _stop_after_turn(agy_home, "kid") is None
    assert _stop_after_turn(agy_home, "par", terminationReason="USER_CANCELED") is None
    assert not busy_path(proc, "kid").exists()
    assert not busy_path(proc, "par").exists()

    with _held_watch_lock(proc, "par"):
        assert _stop_after_turn(agy_home, "par") is None
        assert not busy_path(proc, "par").exists()
    assert _stop_after_turn(agy_home, "par") is not None


def test_stop_backs_off_after_two_declined_asks_then_asks_again_on_schedule(
    agy_home: Path,
) -> None:
    pid = os.getpid()
    for conversation in ("c1", "c2"):
        run_hook("SessionStart", pid, _payload(conversation, agy_home))
    proc = process_dir(pid)
    assert proc is not None

    def declined_ask() -> None:
        assert _stop_after_turn(agy_home, "c1") is not None  # asked
        assert _stop_after_turn(agy_home, "c1") is None  # the continuation ended, no watcher

    declined_ask()
    declined_ask()
    for _ in range(2):  # skips 2 turn ends, then asks once more
        assert _stop_after_turn(agy_home, "c1") is None
    declined_ask()
    for _ in range(4):  # a further miss doubles the skip
        assert _stop_after_turn(agy_home, "c1") is None
    assert _stop_after_turn(agy_home, "c1") is not None
    assert _stop_after_turn(agy_home, "c2") is not None  # a new conversation starts fresh

    with _held_watch_lock(proc, "c1"):
        assert _stop_after_turn(agy_home, "c1") is None  # a running watcher resets it all
    assert _stop_after_turn(agy_home, "c1") is not None


@contextmanager
def _held_watch_lock(proc: Path, conversation: str) -> Generator[None]:
    path = watch_lock_path(proc, conversation)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        os.close(fd)


def test_batch_is_confirmed_at_stop_and_a_lost_one_returns_to_the_inbox(agy_home: Path) -> None:
    pid = os.getpid()
    payload = _payload("c1", agy_home)
    transcript = Path(json.loads(payload)["transcriptPath"])
    run_hook("SessionStart", pid, payload)  # registers; this test is about the inbox
    entry = inbox.enqueue("c1", "[Meridian] Message from spawn p7. Reply.\n\nping", _sender("p7"))

    first = run_hook("PreInvocation", pid, payload)
    assert first is not None
    steps = json.loads(first)["injectSteps"]
    assert [s["systemMessage"]["systemMessage"] for s in steps] == [entry.render()]
    assert entry.sender == _sender("p7")
    assert run_hook("PreInvocation", pid, payload) is None  # nothing more is sent mid-turn

    # The turn ends and the transcript lacks the marker: back to the inbox, sent once more.
    _stop_after_turn(agy_home, "c1")
    assert not inflight_path("c1").exists()
    assert run_hook("PreInvocation", pid, payload) is not None

    transcript.parent.mkdir(parents=True)
    transcript.write_text(f"injected by a PreInvocation hook: [meridian:{entry.id}] ping\n")
    _stop_after_turn(agy_home, "c1")
    assert not inflight_path("c1").exists()
    assert not inbox.inbox_path("c1").exists()


def test_unconfirmed_entry_is_not_resent_after_max_attempts(agy_home: Path) -> None:
    pid = os.getpid()
    payload = _payload("c1", agy_home)
    inbox.enqueue("c1", "ping", None)

    sent: list[bool] = []
    for _ in range(inbox.MAX_ATTEMPTS + 1):
        sent.append(run_hook("PreInvocation", pid, payload) is not None)
        _stop_after_turn(agy_home, "c1")

    assert sent == [True] * inbox.MAX_ATTEMPTS + [False]
    assert inflight_path("c1").exists()  # kept for the notice sweep


def test_confirm_reads_from_the_earliest_batch_left_by_a_turn_without_stop(
    agy_home: Path,
) -> None:
    transcript = agy_home / "t.jsonl"
    transcript.write_text("[meridian:old1] from an older build\n")
    flight = inflight_path("c1")
    flight.parent.mkdir(parents=True)
    flight.write_text('{"id":"old1","text":"x","sender":null,"attempts":1}\n')  # no offset

    cancelled = inbox.enqueue("c1", "cancelled turn", None)
    assert inbox.claim("c1", str(transcript)) != []
    with transcript.open("a") as handle:  # an Esc: the marker landed, no Stop ran
        handle.write(f"[meridian:{cancelled.id}] cancelled turn\n")
    shown = inbox.enqueue("c1", "next turn", None)
    lost = inbox.enqueue("c1", "lost", None)
    inbox.claim("c1", str(transcript))
    with transcript.open("a") as handle:
        handle.write(f"[meridian:{shown.id}] next turn\n")

    inbox.confirm("c1", str(transcript))

    queued = [json.loads(line) for line in inbox.inbox_path("c1").read_text().splitlines()]
    assert [e["id"] for e in queued] == [lost.id]
    assert not inflight_path("c1").exists()


def test_crash_in_claim_then_stop_shows_the_message_once(agy_home: Path) -> None:
    transcript = agy_home / "t.jsonl"
    transcript.write_text("x\n")
    entry = inbox.enqueue("c1", "ping", None)
    batch = inbox.claim("c1", str(transcript))
    # claim was killed before it unlinked the inbox: nothing was injected, the id is in both.
    inbox.inbox_path("c1").write_text(entry.to_line() + "\n")

    inbox.confirm("c1", str(transcript))  # the turn's Stop: no marker

    assert [e.id for e in inbox.claim("c1", str(transcript))] == [entry.id]
    assert len(batch) == 1
    flying = [json.loads(line) for line in inflight_path("c1").read_text().splitlines()]
    assert [(e["id"], e["attempts"]) for e in flying] == [(entry.id, 2)]


def test_exhausted_entry_stays_in_flight_and_confirm_stops_reading_for_it(
    agy_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = agy_home / "t.jsonl"
    transcript.write_text("x\n")
    inbox.enqueue("c1", "never shown", None)
    for _ in range(inbox.MAX_ATTEMPTS):
        inbox.claim("c1", str(transcript))
        inbox.confirm("c1", str(transcript))
    before = inflight_path("c1").read_text()
    assert '"exhausted":true' in before

    reads: list[int] = []
    monkeypatch.setattr(inbox, "_read_transcript", lambda *args: reads.append(args[1]) or b"")
    inbox.confirm("c1", str(transcript))

    assert reads == []
    assert inflight_path("c1").read_text() == before


def test_prompts_are_queued_with_no_sender_only_while_a_watcher_runs(agy_home: Path) -> None:
    import asyncio

    from meridian.lib.harness.agy.idle import AgyIdleSensor

    FAR = 2**62
    proc = agy_home / "proc"
    sensor = AgyIdleSensor(proc, "c1", stored_turn_id=None, report_activity=lambda _activity: None)

    # No watcher: it could not wake the session, so nothing is queued and nothing delivered.
    assert asyncio.run(sensor.send("reminder", stop_offered=False, expires_at=FAR)) is False
    assert inbox.claim("c1", None) == []

    # A busy mark (a running turn, or an Esc that left it) means no wake is possible.
    busy = busy_path(proc, "c1")
    busy.parent.mkdir(parents=True, exist_ok=True)
    busy.touch()
    with _held_watch_lock(proc, "c1"):
        assert asyncio.run(sensor.send("ping", stop_offered=True, expires_at=FAR)) is False
    busy.unlink()

    with _held_watch_lock(proc, "c1"):
        assert asyncio.run(sensor.send("ping", stop_offered=True, expires_at=FAR)) is True
    (entry,) = inbox.claim("c1", None)
    assert entry.sender is None
    assert "stop-waiting" in entry.text


def test_meridian_prompts_expire_when_the_cache_goes_cold(agy_home: Path) -> None:
    import time

    now = int(time.time() * 1000)
    inbox.enqueue("c1", "stale", None, expires_at=now - 1)
    live = inbox.enqueue("c1", "live", None, expires_at=now + 60_000)
    # An older build's line has no ``expires_at``; so does a peer message.
    with inbox.inbox_path("c1").open("a") as handle:
        handle.write('{"id":"old","text":"old","sender":null,"attempts":0}\n')
    peer = inbox.enqueue("c1", "from a peer", _sender("p1"))

    claimed = inbox.claim("c1", None)
    assert [e.text for e in claimed] == ["live", "old", "from a peer"]
    assert "received it" not in live.render()
    assert "say in your reply that you received it" in peer.render()

    # Shown nowhere, so ``confirm`` would re-queue them; the one past its time is dropped.
    inbox.enqueue("c1", "stale", None, expires_at=now - 1)
    inbox.claim("c1", None)
    inbox.confirm("c1", None)
    assert sorted(e.text for e in inbox._read_entries(inbox.inbox_path("c1"))) == [
        "from a peer",
        "live",
        "old",
    ]


def test_claim_gives_up_while_another_process_holds_the_lock(agy_home: Path) -> None:
    from meridian.lib.platform import fcntl

    inbox.enqueue("c1", "ping", None)
    lock_path = inbox._lock_path("c1")  # pyright: ignore[reportPrivateUsage]
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert inbox.claim("c1", None) == []
    finally:
        os.close(fd)
    assert len(inbox.claim("c1", None)) == 1


def test_tool_result_shows_stdout_stderr_and_exit_code() -> None:
    assert format_result("out\n", "", 0) == "out\n[exit code 0]"
    assert format_result("", "bad\n", 2) == "[stderr]\nbad\n[exit code 2]"


def test_tool_call_runs_as_its_root_conversations_row() -> None:
    meta = {
        "antigravity.google/conversation_id": "kid",
        "antigravity.google/root_conversation_id": "root",
    }
    assert root_conversation(meta) == "root"
    assert root_conversation({"antigravity.google/conversation_id": "solo"}) == "solo"
    assert root_conversation({"antigravity.google/root_conversation_id": "../x"}) is None

    row = HeldRow("p4", "c4", Path("/proj"), Path("/runtime"), Path("/proj"))
    env = call_env(row)
    assert (env["MERIDIAN_SPAWN_ID"], env["MERIDIAN_CHAT_ID"]) == ("p4", "c4")
    assert env["MERIDIAN_PROJECT_DIR"] == "/proj"
    assert env["_MERIDIAN_DEPTH"] == "0"


def test_closed_conversation_notifies_only_for_undelivered_messages(
    agy_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        "meridian.lib.harness.agy.notices._send",
        lambda sender, text: sent.append(sender.spawn_id),
    )
    transcript = agy_home / "t.jsonl"
    write_private_json(
        agy_root() / "999999-1" / "requests" / "c1.json",
        {"role": "primary", "transcript_path": str(transcript)},
    )
    seen = inbox.enqueue("c1", "first", _sender("p8"))
    inbox.enqueue("c1", "second", _sender("p9"))
    inbox.enqueue("c1", "third", None)
    inbox.claim("c1", None)  # all go out; the transcript will show only the first
    transcript.write_text(f"[meridian:{seen.id}] first\n")

    send_owed_notices()

    assert sent == ["p9"]
    assert not inflight_path("c1").exists()
    assert not inbox.inbox_path("c1").exists()
    send_owed_notices()
    assert sent == ["p9"]


def test_reopened_hold_releases_its_launch_slot_when_the_hold_fails(
    agy_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from meridian.lib.harness.agy import holds
    from meridian.lib.launch import primary_hold

    class Slot:
        released = False

        def __enter__(self) -> "Slot":
            return self

        def __exit__(self, *exc: object) -> None:
            self.released = True

    slot = Slot()
    spawn = SimpleNamespace(id="p1")
    reopened = holds._Reopen("c1", SimpleNamespace(slot=slot, spawn=spawn))  # pyright: ignore[reportArgumentType]
    monkeypatch.setattr(holds, "_reopen", lambda *_: reopened)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("hold failed")

    monkeypatch.setattr(primary_hold, "primary_hold", fail)

    with pytest.raises(RuntimeError, match="hold failed"):
        holds._hold_row(holds._Hold("conv"), {"cwd": str(agy_home)}, agy_home)
    assert slot.released


def test_launch_directory_handle_files_the_row_under_the_launch_project(
    agy_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = agy_home / "project"
    context = agy_home / "home" / "context" / "p" / "archive" / "work"
    # agy reports cwd as the first of its sorted workspace paths.
    payload = _payload(
        "c1", agy_home, cwd=str(context), workspacePaths=[str(context), str(project)]
    )
    monkeypatch.setenv("_MERIDIAN_AGY_PROJECT", str(project))

    run_hook("SessionStart", os.getpid(), payload)
    proc_dir = process_dir(os.getpid())
    assert proc_dir is not None
    request = read_json(request_path(proc_dir, "c1"))

    assert request is not None
    assert request["cwd"] == str(project)
