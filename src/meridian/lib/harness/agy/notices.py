"""Closed-session notices: tell a sender its message died with an ``agy`` session.

``agy`` exits without waiting for Meridian (its server gets about 400 ms from stdin close
to SIGKILL), and a notice can block for as long as a message send does, so nothing is sent
at exit. The queue files stay, and whoever runs ``send_owed_notices`` next sends the notices:
the next MCP server at startup and primary-launch recovery. A conversation is owed a notice when
no running ``agy`` process has a request file for it; one resumed in a new process is not.
Notices are at most once: the entries are removed before they are sent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from meridian.lib.core.delivery import MessageSender
from meridian.lib.harness.agy import inbox
from meridian.lib.harness.agy.state import (
    process_dirs,
    read_json,
    request_path,
    row_path,
    scrubbed_env,
)
from meridian.lib.platform.python_module import module_argv

_SEND_TIMEOUT_SECONDS = 40.0


@dataclass(frozen=True)
class Notice:
    sender: MessageSender
    text: str


def _notice_text(entry: inbox.InboxEntry, session: str) -> str:
    return (
        f"Your message to {session} was not delivered: its Antigravity session ended before "
        "its next model call, so the message was dropped. Send it again once the session "
        f"is running.\n\nThe message began: {entry.text.partition(chr(10) * 2)[2][:120]}"
    )


def collect_owed_notices() -> list[Notice]:
    """Take every undelivered message of a closed conversation; forget the dead processes."""

    dirs = process_dirs()
    running = {
        path.stem for proc, alive in dirs if alive for path in (proc / "requests").glob("*.json")
    }
    notices: list[Notice] = []
    for proc, alive in dirs:
        if alive:
            continue
        for path in sorted((proc / "requests").glob("*.json")):
            conversation = path.stem
            if conversation in running:
                continue
            request = read_json(request_path(proc, conversation)) or {}
            row = read_json(row_path(proc, conversation)) or {}
            transcript = request.get("transcript_path")
            session = row.get("spawn_id") or "an Antigravity session"
            for entry in inbox.take_undelivered(
                conversation, transcript if isinstance(transcript, str) else None
            ):
                if entry.sender is not None:
                    notices.append(Notice(entry.sender, _notice_text(entry, str(session))))
        shutil.rmtree(proc, ignore_errors=True)
    return notices


def send_owed_notices() -> int:
    """Send the notices collected now; return how many senders were reached."""

    reached = 0
    for notice in collect_owed_notices():
        try:
            done = subprocess.run(
                module_argv(
                    "meridian",
                    "-C",
                    notice.sender.project_root,
                    "spawn",
                    "message",
                    notice.sender.spawn_id,
                    "--message",
                    notice.text,
                ),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_SEND_TIMEOUT_SECONDS,
                check=False,
                env=scrubbed_env(os.environ),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        reached += done.returncode == 0
    return reached


__all__ = ["Notice", "collect_owed_notices", "send_owed_notices"]
