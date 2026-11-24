"""Per-conversation message inbox for ``agy``, drained by the ``PreInvocation`` hook.

``agy`` has no push channel, so a message waits in ``inbox/<conversation>.jsonl`` until
the next model call (an idle session's watcher, ``watch.py``, exits to make one). ``claim``
moves the queue to ``inflight/<conversation>.jsonl``, noting how long the transcript was,
and the model call shows it. The turn's ``Stop`` hook runs ``confirm``: it looks for each
entry's marker in what the transcript gained since, and an entry it lacks goes back to the
inbox, which wakes an idle session. A turn with no ``Stop`` (Esc) leaves its batch in
flight for a later ``Stop``. Delivery is at least once.

Writers take the conversation's lock blocking. The hook never waits long: ``claim`` takes it
without blocking and gives up for this model call when someone holds it, and the hook's
registration and ``confirm`` wait at most ``HOOK_LOCK_WAIT_SECONDS`` (see ``hold``).
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
from typing import cast

from meridian.lib.core.delivery import MessageSender
from meridian.lib.harness.agy.state import agy_root, inbox_path, inflight_path
from meridian.lib.platform import fcntl
from meridian.lib.platform.atomic import PRIVATE_FILE_MODE, atomic_write_text

# A message the transcript never shows is sent this many times, so a transcript Meridian
# cannot read does not fill the model's context forever.
MAX_ATTEMPTS = 3

# How long the hook waits for a conversation's lock to register it or to confirm a batch. The
# notice sweep holds the lock across notice sends (seconds), so the hook gives up and tries
# again on its next call.
HOOK_LOCK_WAIT_SECONDS = 0.5


@dataclass(frozen=True)
class InboxEntry:
    id: str
    text: str
    # None when the message had no sender identity: nobody can be told it was not delivered.
    sender: MessageSender | None
    attempts: int = 0
    # Transcript size when ``claim`` sent it: the marker lands after it. None for an entry
    # an older build wrote, or one sent with no readable transcript: ``confirm`` then reads
    # the whole file.
    offset: int | None = None

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
                **({} if self.offset is None else {"offset": self.offset}),
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
                    offset=None if doc.get("offset") is None else int(doc["offset"]),
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


def claim(conversation_id: str, transcript_path: str | None) -> list[InboxEntry]:
    """Move the queued messages in flight and return them for the hook to inject.

    Returns nothing when the queue is empty or another process holds the lock.
    """

    with _locked(conversation_id, wait=0) as acquired:
        if not acquired:
            return []
        inbox = inbox_path(conversation_id)
        queued = _read_entries(inbox)
        if not queued:
            return []
        offset = _transcript_size(transcript_path)
        batch = [InboxEntry(e.id, e.text, e.sender, e.attempts + 1, offset) for e in queued]
        # In-flight first: a crash between the two steps repeats a batch instead of losing it.
        # The same entry in both files then is one entry: the new copy replaces the old.
        sent = {e.id for e in batch}
        flying = [e for e in _read_entries(inflight_path(conversation_id)) if e.id not in sent]
        _write_entries(inflight_path(conversation_id), [*flying, *batch])
        inbox.unlink(missing_ok=True)
        return batch


def confirm(conversation_id: str, transcript_path: str | None) -> None:
    """Return the in-flight entries the transcript does not show to the inbox.

    Run by the ``Stop`` hook, which is after the turn's model calls: the marker is written
    when ``agy`` injects the message, before the call that shows it. Reads the transcript from
    the earliest offset ``claim`` noted, the whole file when any entry has none or the file
    is shorter than that (a compaction). An entry sent ``MAX_ATTEMPTS`` times is not sent
    again but stays in flight, so the notice sweep tells its sender it was never shown.
    """

    flight = inflight_path(conversation_id)
    if not flight.exists():
        return
    with _locked(conversation_id, wait=HOOK_LOCK_WAIT_SECONDS) as acquired:
        if not acquired:
            return
        flying = _read_entries(flight)
        offsets = [e.offset for e in flying]
        start = 0 if None in offsets else min(cast("list[int]", offsets), default=0)
        transcript = _read_transcript(transcript_path, start)
        missing = [e for e in flying if f"meridian:{e.id}]".encode() not in transcript]
        retry = [e for e in missing if e.attempts < MAX_ATTEMPTS]
        if retry:
            # Inbox first: a crash between the two writes repeats entries, never loses them.
            _write_entries(
                inbox_path(conversation_id), [*retry, *_read_entries(inbox_path(conversation_id))]
            )
        _write_entries(flight, [e for e in missing if e.attempts >= MAX_ATTEMPTS])


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
        transcript = _read_transcript(transcript_path, 0)
        # A crash between ``claim``'s two writes leaves an entry in both files.
        queued = {
            e.id: e
            for e in [
                *_read_entries(inflight_path(conversation_id)),
                *_read_entries(inbox_path(conversation_id)),
            ]
        }
        owed = [e for e in queued.values() if f"meridian:{e.id}]".encode() not in transcript]
        # One file from here on: a crash between these two steps repeats entries, never loses.
        _write_entries(inbox_path(conversation_id), owed)
        inflight_path(conversation_id).unlink(missing_ok=True)
        for position, entry in enumerate(owed, start=1):
            settle(entry)
            _write_entries(inbox_path(conversation_id), owed[position:])


def _transcript_size(transcript_path: str | None) -> int | None:
    try:
        return Path(transcript_path).stat().st_size if transcript_path else None
    except OSError:
        return None


def _read_transcript(transcript_path: str | None, start: int) -> bytes:
    """The transcript from byte ``start``, all of it when it is shorter; empty when unreadable."""

    if not transcript_path:
        return b""
    try:
        with Path(transcript_path).open("rb") as handle:
            if start > handle.seek(0, os.SEEK_END):
                start = 0
            handle.seek(start)
            return handle.read()
    except OSError:
        return b""


__all__ = [
    "HOOK_LOCK_WAIT_SECONDS",
    "MAX_ATTEMPTS",
    "InboxEntry",
    "claim",
    "confirm",
    "enqueue",
    "hold",
    "inbox_path",
    "settle_undelivered",
]
