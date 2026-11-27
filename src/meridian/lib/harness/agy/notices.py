"""Closed-session notices: tell a sender its message died with an ``agy`` session.

``agy`` exits without waiting for Meridian (its server gets about 400 ms from stdin close
to SIGKILL), and a notice can block for as long as a message send does, so nothing is sent
at exit. The queue files stay, and whoever runs ``send_owed_notices`` next sends the notices:
the next MCP server at startup and primary-launch recovery. A conversation is owed a notice when
no running ``agy`` process has a request file for it, checked under the conversation's lock
just before settling it: one resumed in a new process, even mid-sweep, is not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import suppress

from meridian.env_registry import session_free_env
from meridian.lib.core.delivery import MessageSender
from meridian.lib.harness.agy import inbox
from meridian.lib.harness.agy.state import (
    open_conversations,
    process_dirs,
    read_json,
    request_path,
    row_path,
)
from meridian.lib.message.reply import reply_tokens, saved_machine_by_id
from meridian.lib.platform.python_module import module_argv

_SEND_TIMEOUT_SECONDS = 40.0


def _notice_text(entry: inbox.InboxEntry, session: str) -> str:
    return (
        f"Your message to {session} was not delivered: it never reached the model (the "
        "Antigravity session ended first, or its transcript never showed the message). "
        "Send it again if it still matters.\n\n"
        f"The message began: {entry.text.partition(chr(10) * 2)[2][:120]}"
    )


def _send(sender: MessageSender, text: str) -> None:
    """One notice, from the sender's own project. A sender that can't be reached is skipped.

    ``--wake``: the notice reports a delivery failure the sender is owed and nobody is here
    to decide, so a cold sender must not hold it. The entry leaves the inbox once this has
    been tried, so a held notice would be dropped silently.
    """

    address = [sender.spawn_id, "-C", sender.project_root]
    if sender.machine_id is not None:
        # A sender on another machine is answered by the name saved here; with no way back,
        # there is nobody to tell.
        saved = saved_machine_by_id(sender.machine_id)
        tokens = (
            reply_tokens(
                saved,
                sender.reply_id or sender.spawn_id,
                claimed=sender.claimed,
                project_root=sender.project_root,
            )
            if saved is not None
            else None
        )
        if tokens is None:
            return
        address = tokens
    with suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            module_argv("meridian", "message", *address, "--wake", "--message", text),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_SEND_TIMEOUT_SECONDS,
            check=False,
            env=session_free_env(os.environ),
        )


def send_owed_notices() -> None:
    """Tell the sender of each message a closed conversation never showed its model.

    Each entry leaves the inbox only after its notice was attempted, and a dead process's
    directory goes only after all its conversations are settled, so a sweeper that is
    killed (``agy`` gives its server about 400 ms) loses nothing: the next sweeper
    repeats at most the notice in progress. A sender that is no longer running cannot be
    reached; its notice is dropped with the entry.
    """

    for proc, alive in process_dirs():
        if alive:
            continue
        for path in sorted((proc / "requests").glob("*.json")):
            conversation = path.stem
            request = read_json(request_path(proc, conversation)) or {}
            row = read_json(row_path(proc, conversation)) or {}
            transcript = request.get("transcript_path")
            session = str(row.get("spawn_id") or "an Antigravity session")

            def settle(entry: inbox.InboxEntry, session: str = session) -> None:
                if entry.sender is not None:
                    _send(entry.sender, _notice_text(entry, session))

            inbox.settle_undelivered(
                conversation,
                transcript if isinstance(transcript, str) else None,
                settle,
                lambda conversation=conversation: conversation in open_conversations(),
            )
        shutil.rmtree(proc, ignore_errors=True)


__all__ = ["send_owed_notices"]
