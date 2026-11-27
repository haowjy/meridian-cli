"""The line a primary reads at a fresh launch when messages are waiting for it."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from meridian.lib.inbox.models import Message
from meridian.lib.inbox.store import check_format, inbox_dir, read_doc, read_issued
from meridian.lib.state.project_identity import Committed, resolve_project_identity

_COUNT_WINDOW: Final = timedelta(days=7)
_NUMBERS_LOOKED_AT: Final = 200
_COUNT_SHOWN: Final = 50


def waiting_count(project: str | None, now: datetime) -> int:
    """Waiting messages newer than 7 days that a session in ``project`` may see, counted
    to one past the number shown.

    Walks down from the highest number issued, so the cost follows recent messages and
    not everything that ever waited: a message's age is its file's time (written once,
    never changed), a number with no file is skipped, and the walk stops at the first
    file older than the window or after 200 numbers.
    """

    root = inbox_dir()
    check_format(root)
    issued = read_issued(root)
    if issued is None:
        return 0
    oldest = (now - _COUNT_WINDOW).timestamp()
    count = 0
    for number in range(issued, max(issued - _NUMBERS_LOOKED_AT, 0), -1):
        path = root / "messages" / f"m{number}.json"
        try:
            if path.stat().st_mtime < oldest:
                break
        except FileNotFoundError:
            continue
        if (root / "taken" / path.name).exists():
            continue
        message = read_doc(path, Message, quiet=True)
        if message is not None and message.project in (None, project):
            count += 1
            if count > _COUNT_SHOWN:
                break
    return count


def launch_line(project_root: Path, now: datetime) -> str | None:
    """The guidance line for a primary launched in ``project_root``, or ``None`` when
    nothing is waiting for it. It is a hint: any failure leaves it out and never fails a
    launch."""

    try:
        if not inbox_dir().is_dir():
            return None
        identity = resolve_project_identity(project_root)
        count = waiting_count(identity.id if isinstance(identity, Committed) else None, now)
    except Exception:
        return None
    if count == 0:
        return None
    if count > _COUNT_SHOWN:
        waiting = f"{_COUNT_SHOWN}+ messages are"
    else:
        waiting = "1 message is" if count == 1 else f"{count} messages are"
    return f"{waiting} waiting in this machine's inbox: `meridian inbox list`."


__all__ = ["launch_line", "waiting_count"]
