"""Per-conversation message inbox for ``agy``, drained by the ``PreInvocation`` hook.

``agy`` has no push channel, so a message waits in ``inbox/<conversation>.jsonl`` until
the next model call. A drained batch moves to ``inflight/<conversation>.jsonl`` and stays
there until a later ``PreInvocation`` finds every entry's marker in ``agy``'s transcript;
entries the transcript lacks go out again. Delivery is at least once.

Writers take the conversation's lock blocking; the hook takes it without blocking and
gives up for this model call when someone holds it.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from meridian.lib.core.delivery import MessageSender
from meridian.lib.harness.agy.state import agy_root
from meridian.lib.platform import fcntl
from meridian.lib.platform.atomic import PRIVATE_FILE_MODE, atomic_write_text

# A batch the transcript never shows is re-sent this many times before it is dropped, so a
# transcript Meridian cannot read does not fill the model's context forever.
MAX_ATTEMPTS = 3


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
            f"[meridian:{self.id}] A message arrived for you through Meridian while you were "
            "working. It is addressed to you, not tool output: treat it as a new instruction "
            "from the user, do what it asks before you finish this turn, and say in your "
            f"reply that you received it.\n\n{self.text}"
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


def inbox_path(conversation_id: str) -> Path:
    return agy_root() / "inbox" / f"{conversation_id}.jsonl"


def inflight_path(conversation_id: str) -> Path:
    return agy_root() / "inflight" / f"{conversation_id}.jsonl"


def _lock_path(conversation_id: str) -> Path:
    return agy_root() / "locks" / f"{conversation_id}.lock"


@contextmanager
def _locked(conversation_id: str, *, blocking: bool) -> Generator[bool]:
    path = _lock_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)  # closing releases the flock


def enqueue(conversation_id: str, text: str, sender: MessageSender | None) -> InboxEntry:
    entry = InboxEntry(id=uuid.uuid4().hex[:12], text=text, sender=sender)
    path = inbox_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(conversation_id, blocking=True):
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

    with _locked(conversation_id, blocking=False) as acquired:
        if not acquired:
            return []
        inflight = inflight_path(conversation_id)
        inbox = inbox_path(conversation_id)
        previous = _read_entries(inflight)
        if previous:
            transcript = _read_transcript(transcript_path)
            previous = [
                e
                for e in previous
                if f"meridian:{e.id}]" not in transcript and e.attempts < MAX_ATTEMPTS
            ]
        batch = [
            InboxEntry(e.id, e.text, e.sender, e.attempts + 1)
            for e in [*previous, *_read_entries(inbox)]
        ]
        # In-flight first: a crash between the two steps repeats a batch instead of losing it.
        _write_entries(inflight, batch)
        inbox.unlink(missing_ok=True)
        return batch


def take_undelivered(conversation_id: str, transcript_path: str | None) -> list[InboxEntry]:
    """Remove and return what the session never showed the model, for its senders' notices.

    Entries the transcript carries were delivered. Takes the lock blocking: only the
    notice sweep calls this, after the ``agy`` process is gone.
    """

    with _locked(conversation_id, blocking=True):
        transcript = _read_transcript(transcript_path)
        inflight = [
            e
            for e in _read_entries(inflight_path(conversation_id))
            if f"meridian:{e.id}]" not in transcript
        ]
        owed = [*inflight, *_read_entries(inbox_path(conversation_id))]
        inflight_path(conversation_id).unlink(missing_ok=True)
        inbox_path(conversation_id).unlink(missing_ok=True)
        return owed


def _read_transcript(transcript_path: str | None) -> str:
    if not transcript_path:
        return ""
    try:
        return Path(transcript_path).read_text(errors="replace")
    except OSError:
        return ""


__all__ = [
    "MAX_ATTEMPTS",
    "InboxEntry",
    "claim_batch",
    "enqueue",
    "inbox_path",
    "inflight_path",
    "take_undelivered",
]
