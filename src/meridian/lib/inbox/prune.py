"""Deleting what the inbox no longer needs: messages taken long ago, records whose
message is gone, and temporary names a killed writer left.

It is garbage collection, not recovery: every state a killed writer can leave is already
a valid one. So it runs at most once a day, from the detached process that fires
``inbox.received`` and from every ``meridian inbox`` command, never at a launch.
"""

from __future__ import annotations

import os
import time
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from meridian.lib.inbox.models import Claim, Message
from meridian.lib.inbox.store import (
    check_format,
    inbox_dir,
    locked,
    numbered,
    origin_link,
    read_doc,
)
from meridian.lib.platform.atomic import atomic_write_text, fsync_directory

TAKEN_RETENTION: Final = timedelta(days=30)
_PRUNE_INTERVAL_SECONDS: Final = 24 * 3600.0
_TEMP_MAX_AGE_SECONDS: Final = 3600.0
# What ``write_model_once``, ``atomic_replace`` and ``atomic_symlink`` name their staged
# files; no file the inbox keeps ends this way.
_TEMP_SUFFIXES: Final = (".tmp", ".create")
_RECORD_DIRS: Final = ("taken", "offered", "hook-log")


def prune_if_due(now: datetime) -> None:
    """Prune and sweep temp names when the daily stamp is older than a day. Never raises."""

    with suppress(Exception):
        root = inbox_dir()
        if not root.is_dir():
            return
        check_format(root)
        stamp = root / "pruned"
        with suppress(FileNotFoundError):
            if now.timestamp() - stamp.stat().st_mtime < _PRUNE_INTERVAL_SECONDS:
                return
        # Stamped first, so the commands that run meanwhile don't each start a pass.
        atomic_write_text(stamp, f"{now.isoformat()}\n", durable=False)
        prune(now)


def prune(now: datetime) -> None:
    """One pass. The scan takes no lock; each deletion takes it and checks again, so
    writers never wait for more than one message's worth of unlinks."""

    root = inbox_dir()
    for message_id in numbered("taken"):
        claim = read_doc(root / "taken" / f"{message_id}.json", Claim, quiet=True)
        if claim is not None and now - claim.at > TAKEN_RETENTION:
            _delete_taken(root, message_id, now)
    for kind in _RECORD_DIRS:
        for message_id in numbered(kind):
            if not (root / "messages" / f"{message_id}.json").exists():
                _delete_orphan(root, root / kind / f"{message_id}.json", message_id)
    _delete_dangling_origin_links(root)
    _sweep_temp_names(root)


def _delete_taken(root: Path, message_id: str, now: datetime) -> None:
    """Delete a message taken more than 30 days ago. The claim goes last: a kill part way
    leaves records with no message, which the next pass removes, and never a message
    that looks waiting again."""

    name = f"{message_id}.json"
    with locked(root):
        claim = read_doc(root / "taken" / name, Claim, quiet=True)
        if claim is None or now - claim.at <= TAKEN_RETENTION:
            return
        message = read_doc(root / "messages" / name, Message, quiet=True)
        (root / "messages" / name).unlink(missing_ok=True)
        # Durable before the claim goes, or a power cut could bring the message back unclaimed.
        with suppress(FileNotFoundError):
            fsync_directory(root / "messages")
        if message is not None and message.via is not None and message.via.ref is not None:
            origin_link(root, message.via.program, message.via.ref).unlink(missing_ok=True)
        (root / "offered" / name).unlink(missing_ok=True)
        (root / "hook-log" / name).unlink(missing_ok=True)
        (root / "taken" / name).unlink(missing_ok=True)


def _delete_orphan(root: Path, record: Path, message_id: str) -> None:
    with locked(root):
        if not (root / "messages" / f"{message_id}.json").exists():
            record.unlink(missing_ok=True)


def _delete_dangling_origin_links(root: Path) -> None:
    directory = root / "by-origin"
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return
    for name in names:
        link = directory / name
        if name.endswith(_TEMP_SUFFIXES) or link.exists():
            continue
        # A writer makes the link before its message, under the lock: look again under it.
        with locked(root):
            if link.is_symlink() and not link.exists():
                link.unlink(missing_ok=True)


def _sweep_temp_names(root: Path) -> None:
    cutoff = time.time() - _TEMP_MAX_AGE_SECONDS
    for directory in (root, *(root / kind for kind in ("messages", "by-origin", *_RECORD_DIRS))):
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            continue
        for name in names:
            if not name.endswith(_TEMP_SUFFIXES):
                continue
            with suppress(OSError):
                if (directory / name).lstat().st_mtime < cutoff:
                    (directory / name).unlink()


__all__ = ["TAKEN_RETENTION", "prune", "prune_if_due"]
