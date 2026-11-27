"""The machine inbox. Placeholder for the store: only what the hook event calls."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from pathlib import Path

from meridian.lib.inbox.models import HookLog
from meridian.lib.platform.atomic import PRIVATE_FILE_MODE, atomic_write_text
from meridian.lib.state.user_paths import get_user_home


def inbox_dir() -> Path:
    return get_user_home() / "machine" / "inbox"


def write_hook_log(message_id: str, log: HookLog) -> None:
    with suppress(OSError):
        atomic_write_text(
            inbox_dir() / "hook-log" / f"{message_id}.json",
            log.model_dump_json(),
            permissions=PRIVATE_FILE_MODE,
        )


def prune_if_due(now: datetime) -> None:
    _ = now
