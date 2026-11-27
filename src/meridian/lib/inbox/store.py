"""The machine inbox's files: ``~/.meridian/machine/inbox/``. Nothing else writes there.

Every change holds ``inbox.lock`` and follows the sequence in ``.context/CONTEXT.md``
step for step: several processes write at once and any of them can be killed between two
system calls. A read takes no lock, because every file appears whole or not at all.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from meridian.lib.inbox.models import Claim, HookLog, Message, Offer, Offers, Party, Via
from meridian.lib.platform.atomic import (
    PRIVATE_FILE_MODE,
    atomic_symlink,
    atomic_write_text,
    fsync_directory,
)
from meridian.lib.platform.locking import FileLockTimeout, lock_file
from meridian.lib.state.json_doc import mutate_model, write_model, write_model_once
from meridian.lib.state.machine_identity import machine_identity_or_none
from meridian.lib.state.user_paths import get_user_home

FORMAT_VERSION: Final = "1"
LOCK_TIMEOUT_SECONDS: Final = 5.0
MESSAGE_ID: Final = re.compile(r"m(0|[1-9][0-9]*)", re.ASCII)
# A listing matches this exactly, so a staged name is never read as a message.
_MESSAGE_FILE: Final = re.compile(r"(m(?:0|[1-9][0-9]*))\.json", re.ASCII)


class InboxError(Exception):
    """An inbox operation that changed nothing. ``kind`` is one of ``meridian message``'s
    error kinds, or ``taken``."""

    kind: str = "failed"

    def __init__(self, text: str, *, kind: str | None = None) -> None:
        super().__init__(text)
        if kind is not None:
            self.kind = kind


class InboxVersionError(InboxError):
    """A newer Meridian wrote the directory."""

    kind = "version"


class MaybeFiled(InboxError):
    """The message file was linked and a later step failed: it may be in the inbox. The
    one error after which something may have been written."""

    kind = "maybe_delivered"


class NoSuchMessage(InboxError):
    kind = "not_found"

    def __init__(self, message_id: str) -> None:
        super().__init__(f"no message {message_id} in this machine's inbox")


class MessageTaken(InboxError):
    """Someone else holds the message; ``claim`` is ``None`` when their file can't be read."""

    kind = "taken"

    def __init__(self, message_id: str, claim: Claim | None) -> None:
        super().__init__(f"{message_id} is taken")
        self.message_id = message_id
        self.claim = claim


@dataclass(frozen=True)
class Entry:
    """One message with what the inbox knows about it."""

    id: str
    message: Message
    claim: Claim | None
    offers: tuple[Offer, ...]


def inbox_dir() -> Path:
    return get_user_home() / "machine" / "inbox"


def message_path(message_id: str) -> Path:
    return inbox_dir() / "messages" / f"{message_id}.json"


def message_number(message_id: str) -> int:
    if MESSAGE_ID.fullmatch(message_id) is None:
        raise InboxError(f"`{message_id}` is not a message ID: one looks like m412", kind="usage")
    return int(message_id[1:])


def check_format(root: Path) -> None:
    """Refuse a directory whose ``format`` this build doesn't know. A missing file is
    format 1: a writer creates it before anything else."""

    format_file = root / "format"
    try:
        found = format_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    if found != FORMAT_VERSION:
        raise InboxVersionError(
            f"{format_file} says format {found!r}, which this Meridian does not know "
            f"(it knows {FORMAT_VERSION!r}): a newer Meridian wrote the inbox. Upgrade Meridian."
        )


@contextmanager
def locked(root: Path) -> Generator[None]:
    """Hold ``inbox.lock`` for one change. The lock is a leaf: take no other lock inside,
    and never unlink it (``lib/state/AGENTS.md``, "Lock Order")."""

    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "inbox.lock"
    try:
        with lock_file(lock_path, timeout=LOCK_TIMEOUT_SECONDS, reentrant=False):
            check_format(root)
            if not (root / "format").exists():
                atomic_write_text(root / "format", f"{FORMAT_VERSION}\n")
            yield
    except FileLockTimeout as exc:
        raise InboxError(
            f"the inbox is busy: {lock_path} was not free within {LOCK_TIMEOUT_SECONDS:g}s. "
            "Nothing was changed; run the command again."
        ) from exc


def read_doc[ModelT: BaseModel](
    path: Path, model_type: type[ModelT], *, quiet: bool = False
) -> ModelT | None:
    """The file's document, or ``None`` when it is missing or doesn't parse. One that
    doesn't parse is named on stderr and never stops the caller."""

    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return model_type.model_validate_json(data)
    except ValidationError:
        if not quiet:
            print(f"inbox: skipped {path}: it does not parse", file=sys.stderr)
        return None


def _numbered(directory: Path) -> list[str]:
    """The message IDs that have a file in ``directory``, lowest number first."""

    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    ids = [match[1] for name in names if (match := _MESSAGE_FILE.fullmatch(name))]
    return sorted(ids, key=lambda message_id: int(message_id[1:]))


def numbered(kind: str) -> list[str]:
    """The message IDs with a file under ``messages``, ``taken``, ``offered`` or ``hook-log``."""

    return _numbered(inbox_dir() / kind)


def read_issued(root: Path) -> int | None:
    """The highest number issued, ``None`` when the file is missing or unreadable."""

    try:
        raw = (root / "issued").read_text(encoding="ascii").strip()
    except (FileNotFoundError, UnicodeDecodeError):
        return None
    return int(raw) if raw.isdecimal() else None


def _issue_number(root: Path) -> int:
    """Issue the next number: written back durably before anything uses it, so a kill
    leaves a gap and never a reuse."""

    issued = read_issued(root)
    if issued is None:
        if any(_numbered(root / kind) for kind in ("messages", "taken", "offered")):
            # Counting up from the files that remain could reissue a pruned number.
            raise InboxError(
                f"{root / 'issued'} is missing or unreadable while the inbox holds messages, "
                "so no number can be issued safely. Restore it with a number above every "
                "message ID this inbox has used."
            )
        issued = 0
    number = issued + 1
    atomic_write_text(root / "issued", f"{number}\n", permissions=PRIVATE_FILE_MODE)
    return number


def origin_link(root: Path, program: str, ref: str) -> Path:
    digest = hashlib.sha256(f"{program}\0{ref}".encode()).hexdigest()
    return root / "by-origin" / digest


def _origin_match(root: Path, via: Via) -> str | None:
    """The message already filed for this program and ref. The link alone proves nothing:
    a kill between the link and the file leaves it naming a message that isn't there."""

    assert via.ref is not None
    try:
        target = os.readlink(origin_link(root, via.program, via.ref))
    except OSError:
        return None
    match = _MESSAGE_FILE.fullmatch(os.path.basename(target))
    if match is None or target != f"../messages/{match[0]}":
        return None
    found = read_doc(root / "messages" / match[0], Message)
    if found is None or found.via is None:
        return None
    return match[1] if (found.via.program, found.via.ref) == (via.program, via.ref) else None


def file_message(message: Message) -> str:
    """Write ``message`` and return its ID. The same outside message (``via`` program and
    ref) filed twice is one message: the second call writes nothing and returns the first
    one's ID."""

    root = inbox_dir()
    via = message.via if message.via is not None and message.via.ref is not None else None
    with locked(root):
        message_id = _origin_match(root, via) if via is not None else None
        if message_id is None:
            message_id = f"m{_issue_number(root)}"
            path = root / "messages" / f"{message_id}.json"
            if via is not None:
                link = origin_link(root, via.program, via.ref or "")
                atomic_symlink(link, Path("..") / "messages" / path.name)
                fsync_directory(link.parent)
            try:
                written = write_model_once(path, message, exclude_none=True)
            except OSError as exc:
                if os.path.lexists(path):
                    raise MaybeFiled(
                        f"{message_id} was written but could not be made durable ({exc}); "
                        "it may be in the inbox and was not filed again"
                    ) from exc
                raise
            if not written:
                raise InboxError(
                    f"{path} already exists though {root / 'issued'} had not reached its "
                    "number. Nothing was written; set `issued` above every message ID."
                )
    return message_id


def read_message(message_id: str) -> Message:
    message_number(message_id)
    root = inbox_dir()
    check_format(root)
    return _require_message(root, message_id)


def _require_message(root: Path, message_id: str) -> Message:
    path = root / "messages" / f"{message_id}.json"
    message = read_doc(path, Message, quiet=True)
    if message is None:
        if path.exists():
            raise InboxError(f"{path} does not parse, so {message_id} can't be read")
        raise NoSuchMessage(message_id)
    return message


def entry(message_id: str) -> Entry:
    """One message with its claim and offers."""

    message = read_message(message_id)
    root = inbox_dir()
    offers = read_doc(root / "offered" / f"{message_id}.json", Offers)
    return Entry(
        message_id,
        message,
        read_doc(root / "taken" / f"{message_id}.json", Claim),
        offers.offers if offers is not None else (),
    )


def entries(*, taken: bool) -> list[Entry]:
    """Waiting messages, or taken ones, oldest first. A claim with no message is skipped."""

    root = inbox_dir()
    check_format(root)
    held = set(_numbered(root / "taken"))
    found: list[Entry] = []
    for message_id in _numbered(root / "messages"):
        if (message_id in held) != taken:
            continue
        message = read_doc(root / "messages" / f"{message_id}.json", Message)
        if message is None:
            continue
        claim = read_doc(root / "taken" / f"{message_id}.json", Claim) if taken else None
        offers = read_doc(root / "offered" / f"{message_id}.json", Offers)
        found.append(Entry(message_id, message, claim, offers.offers if offers else ()))
    return found


def take(message_id: str, by: Party, via: Via | None, now: datetime) -> Entry:
    """Claim the message for ``by``. The same taker asking again succeeds, so a call cut
    off after it was sent can be repeated; anyone else gets ``MessageTaken``."""

    message_number(message_id)
    root = inbox_dir()
    path = root / "taken" / f"{message_id}.json"
    with locked(root):
        message = _require_message(root, message_id)
        claim = Claim(by=by, via=via, at=now)
        if not write_model_once(path, claim, exclude_none=True):
            held = read_doc(path, Claim)
            if held is None or not held.same_taker(by, via, machine_identity_or_none()):
                raise MessageTaken(message_id, held)
            claim = held
        offers = read_doc(root / "offered" / f"{message_id}.json", Offers)
    return Entry(message_id, message, claim, offers.offers if offers is not None else ())


def release(message_id: str, by: Party) -> bool:
    """Give a claim back; the message is waiting again. A terminal may release any claim,
    a session only its own. False when the message was not taken, which is a success: a
    release cut off after it was sent can be repeated."""

    message_number(message_id)
    root = inbox_dir()
    path = root / "taken" / f"{message_id}.json"
    with locked(root):
        if not os.path.lexists(path):
            _require_message(root, message_id)
            return False
        held = read_doc(path, Claim)
        if by.kind != "terminal" and (
            held is None or not held.by.same_as(by, machine_identity_or_none())
        ):
            raise MessageTaken(message_id, held)
        path.unlink()
        fsync_directory(path.parent)
    return True


def waiting_message(message_id: str) -> Message:
    """The message, when it exists and nobody holds it: what ``offer`` checks before it
    sends. The lock is not held across the send."""

    message_number(message_id)
    root = inbox_dir()
    with locked(root):
        return _waiting(root, message_id)


def _waiting(root: Path, message_id: str) -> Message:
    message = _require_message(root, message_id)
    path = root / "taken" / f"{message_id}.json"
    if os.path.lexists(path):
        raise MessageTaken(message_id, read_doc(path, Claim))
    return message


def record_offer(message_id: str, to: str, now: datetime) -> None:
    """Append to the message's offer record, after its notice was delivered. Raises as
    ``waiting_message`` does when the message was taken or deleted meanwhile."""

    root = inbox_dir()

    def append(current: Offers | None) -> tuple[Offers, None]:
        _waiting(root, message_id)
        offers = current.offers if current is not None else ()
        return Offers(offers=(*offers, Offer(to=to, at=now))), None

    mutate_model(root / "offered" / f"{message_id}.json", Offers, append, lock=locked(root))


def write_hook_log(message_id: str, log: HookLog) -> None:
    """Replace hook-log/<message_id>.json atomically, mode 0600. Never raises."""

    with suppress(Exception):
        message_number(message_id)
        write_model(inbox_dir() / "hook-log" / f"{message_id}.json", log)


__all__ = [
    "FORMAT_VERSION",
    "MESSAGE_ID",
    "Entry",
    "InboxError",
    "InboxVersionError",
    "MaybeFiled",
    "MessageTaken",
    "NoSuchMessage",
    "check_format",
    "entries",
    "entry",
    "file_message",
    "inbox_dir",
    "locked",
    "message_number",
    "message_path",
    "numbered",
    "origin_link",
    "read_doc",
    "read_issued",
    "read_message",
    "record_offer",
    "release",
    "take",
    "waiting_message",
    "write_hook_log",
]
