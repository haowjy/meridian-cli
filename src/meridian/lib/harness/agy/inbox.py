"""Per-conversation message inbox for ``agy``, drained by the ``PreInvocation`` hook.

``agy`` has no push channel, so a message waits in ``inbox/<conversation>.jsonl`` until
the next model call (an idle session's watcher, ``watch.py``, exits to make one). A drained
batch moves to ``inflight/<conversation>.jsonl`` and stays there until a later
``PreInvocation`` finds every entry's marker in ``agy``'s transcript; entries the transcript
lacks go out again. Delivery is at least once.

Writers take the conversation's lock blocking. The hook never waits long: ``claim_batch``
takes it without blocking and gives up for this model call when someone holds it, and the
hook's registration waits at most ``REGISTER_WAIT_SECONDS`` (see ``hold``).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from meridian.lib.core.delivery import MessageSender
from meridian.lib.harness.agy.state import agy_root, inbox_path, inflight_path
from meridian.lib.platform import fcntl
from meridian.lib.platform.atomic import PRIVATE_FILE_MODE, atomic_write_text

# A message the transcript never shows is sent this many times, so a transcript Meridian
# cannot read does not fill the model's context forever.
MAX_ATTEMPTS = 3

# How long the hook waits for a conversation's lock to register it. The notice sweep holds
# the lock across notice sends (seconds), so the hook gives up and registers on its next call.
REGISTER_WAIT_SECONDS = 0.5


@dataclass(frozen=True)
class InboxEntry:
    id: str
    text: str
    # None when the message had no sender identity: nobody can be told it was not delivered.
    sender: MessageSender | None
    attempts: int = 0

    def render(self) -> str:
        # A hook's system message is easy for a model to read past (a GPT-OSS run ignored
        # one), so the opening says it is addressed to the model and what to do.
        return (
            f"[meridian:{self.id}] A message arrived for you through Meridian. It is an "
            "instruction addressed to this session from the session named below, not from "
            "the user and not tool output: do what it asks before you finish this turn, and "
            f"say in your reply that you received it.\n\n{self.text}"
        )

    def to_line(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "text": self.text,
                "sender": asdict(self.sender) if self.sender else None,
                "attempts": self.attempts,
            },
            separators=(",", ":"),
        )


def _lock_path(conversation_id: str) -> Path:
    return agy_root() / "locks" / f"{conversation_id}.lock"


@contextmanager
def _locked(conversation_id: str, *, wait: float | None) -> Generator[bool]:
    """Hold the conversation's lock; ``wait`` is seconds to wait for it, None to block."""

    path = _lock_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        deadline = None if wait is None else time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if deadline is None else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if deadline is None or time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.02)
        yield True
    finally:
        os.close(fd)  # closing releases the flock


def hold(conversation_id: str, *, wait: float) -> AbstractContextManager[bool]:
    """Take the conversation's lock for up to ``wait`` seconds; yields whether it was taken.

    The hook registers a conversation's request file under it, so the notice sweep, which
    checks for that file under the same lock, never settles a conversation that a new
    process has just opened.
    """

    return _locked(conversation_id, wait=wait)


def enqueue(conversation_id: str, text: str, sender: MessageSender | None) -> InboxEntry:
    entry = InboxEntry(id=uuid.uuid4().hex[:12], text=text, sender=sender)
    path = inbox_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(conversation_id, wait=None):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, (entry.to_line() + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    return entry


def _read_entries(path: Path) -> list[InboxEntry]:
    try:
        raw = path.read_text()
    except OSError:
        return []
    entries: list[InboxEntry] = []
    for line in raw.splitlines():
        try:
            doc = json.loads(line)
            entries.append(
                InboxEntry(
                    id=str(doc["id"]),
                    text=str(doc["text"]),
                    sender=MessageSender(**doc["sender"]) if doc.get("sender") else None,
                    attempts=int(doc.get("attempts", 0)),
                )
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            continue  # a torn line from a crash mid-write
    return entries


def _write_entries(path: Path, entries: list[InboxEntry]) -> None:
    if not entries:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        "".join(entry.to_line() + "\n" for entry in entries),
        durable=False,
        permissions=PRIVATE_FILE_MODE,
    )


def claim_batch(conversation_id: str, transcript_path: str | None) -> list[InboxEntry]:
    """Confirm the last batch against the transcript, then claim the next one.

    Returns what the hook must inject: unconfirmed entries of the previous batch plus
    everything queued since. Returns nothing when another process holds the lock.
    """

    with _locked(conversation_id, wait=0) as acquired:
        if not acquired:
            return []
        inflight = inflight_path(conversation_id)
        inbox = inbox_path(conversation_id)
        previous = _read_entries(inflight)
        if previous:
            transcript = _read_transcript(transcript_path)
            previous = [e for e in previous if f"meridian:{e.id}]" not in transcript]
        # An entry sent MAX_ATTEMPTS times is not sent again but stays in flight, so the
        # notice sweep tells its sender it was never shown.
        exhausted = [e for e in previous if e.attempts >= MAX_ATTEMPTS]
        batch = [
            InboxEntry(e.id, e.text, e.sender, e.attempts + 1)
            for e in [*(e for e in previous if e.attempts < MAX_ATTEMPTS), *_read_entries(inbox)]
        ]
        # In-flight first: a crash between the two steps repeats a batch instead of losing it.
        _write_entries(inflight, [*exhausted, *batch])
        inbox.unlink(missing_ok=True)
        return batch


def settle_undelivered(
    conversation_id: str,
    transcript_path: str | None,
    settle: Callable[[InboxEntry], None],
    is_open: Callable[[], bool],
) -> None:
    """Call ``settle`` for each message the session never showed the model, then forget it.

    Does nothing when ``is_open()`` is true. It is asked under the conversation's lock, which
    the hook's registration also takes, so a conversation a new process opens after a
    sweep began is never settled.

    Entries the transcript carries were delivered. An entry is removed only after its
    ``settle`` returns, so a sweeper killed halfway leaves the rest (and at most the entry
    in progress, which the next sweeper repeats). Holds the lock blocking, across every
    ``settle``: only the notice sweep calls this, after the ``agy`` process is gone.
    """

    with _locked(conversation_id, wait=None):
        if is_open():
            return
        transcript = _read_transcript(transcript_path)
        owed = [
            e
            for e in [
                *_read_entries(inflight_path(conversation_id)),
                *_read_entries(inbox_path(conversation_id)),
            ]
            if f"meridian:{e.id}]" not in transcript
        ]
        # One file from here on: a crash between these two steps repeats entries, never loses.
        _write_entries(inbox_path(conversation_id), owed)
        inflight_path(conversation_id).unlink(missing_ok=True)
        for position, entry in enumerate(owed, start=1):
            settle(entry)
            _write_entries(inbox_path(conversation_id), owed[position:])


def _read_transcript(transcript_path: str | None) -> str:
    if not transcript_path:
        return ""
    try:
        return Path(transcript_path).read_text(errors="replace")
    except OSError:
        return ""


__all__ = [
    "MAX_ATTEMPTS",
    "REGISTER_WAIT_SECONDS",
    "InboxEntry",
    "claim_batch",
    "enqueue",
    "hold",
    "inbox_path",
    "inflight_path",
    "settle_undelivered",
]
