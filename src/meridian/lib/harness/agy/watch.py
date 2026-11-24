"""``meridian harness-watch agy``: the background command that wakes an idle ``agy`` session.

``agy`` starts a model call when a background command the agent started exits, so the
agent keeps this one running and it exits when there is a reason to wake the session: a
message arrived while no turn runs. It decides from files in the ``agy`` process's
directory and the inbox, and holds a read-only ``flock`` on ``watch/<conversation>.lock``
for as long as it lives, so the ``Stop`` hook asks for a watcher only when none runs. It
may run inside ``agy``'s terminal sandbox, so it writes nothing. It prints a notice, not
the message: the message stays on the ``PreInvocation`` delivery path.

Imports only ``state.py``: the hook that tests the lock runs at the end of every turn.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from meridian.lib.harness.agy.state import (
    busy_path,
    inbox_path,
    is_safe_id,
    process_birth,
    process_dir,
    watch_lock_path,
)
from meridian.lib.platform import fcntl

POLL_SECONDS = 1.0
# The ``agy`` process check costs a ``ps`` on macOS, so it runs every few polls.
_ALIVE_EVERY_POLLS = 10
# The ``Stop`` hook holds the lock for an instant when it tests it. Short enough that a
# duplicate start finishes inside the 500 ms ``WaitMsBeforeAsync`` the ask names: a
# command that goes to the background wakes the agent when it exits.
_LOCK_WAIT_SECONDS = 0.25


def watch_command(agy_pid: int, conversation: str) -> str:
    return f"meridian harness-watch agy --agy-pid {agy_pid} --conversation {conversation}"


def start_instructions(agy_pid: int, conversation: str) -> str:
    """How the model starts the watcher; the ``Stop`` reply and the wake notice say it."""

    return (
        f"Run `{watch_command(agy_pid, conversation)}` with run_command, WaitMsBeforeAsync "
        "500, IsDaemon false and BypassSandbox true (it reads Meridian's state, which the "
        "sandbox hides), so it runs in the background, then end your turn without waiting "
        "for it or polling it."
    )


_CANNOT_WATCH = (
    "This watcher cannot see the agy process or Meridian's state, so it cannot watch: it "
    "most likely ran inside agy's terminal sandbox, which has its own process list and hides "
    "~/.meridian. Start it again with run_command and BypassSandbox true."
)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def watcher_running(proc_dir: Path, conversation: str) -> bool:
    """Whether a process holds the conversation's watch lock (for the ``Stop`` hook)."""

    path = watch_lock_path(proc_dir, conversation)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        return not _try_lock(fd)
    finally:
        os.close(fd)  # closing releases a lock taken by the test


def _inbox_signature(conversation: str) -> tuple[int, int, int] | None:
    """Identity of the inbox's current content, None while it is missing or empty."""

    try:
        stat = inbox_path(conversation).stat()
    except OSError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns) if stat.st_size > 0 else None


def run(agy_pid: int, conversation: str) -> int:
    """Run until the session is idle and has a reason to wake; always returns 0."""

    if not is_safe_id(conversation):
        return 0
    parent = os.getppid()
    birth = process_birth(agy_pid)
    proc_dir = process_dir(agy_pid)
    # Opened without creating it: the ``Stop`` hook does, and a sandbox that mounts the
    # state read-only must end in the notice, not a traceback. A dead ``agy`` has no reader
    # for the notice, so it goes to a live one.
    try:
        if birth is None or proc_dir is None:
            raise OSError
        fd = os.open(watch_lock_path(proc_dir, conversation), os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        print(_CANNOT_WATCH)
        return 0
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while not _try_lock(fd):
        if time.monotonic() >= deadline:
            print("A watcher for this conversation already runs; there is nothing to start.")
            return 0
        time.sleep(0.05)

    noted = _inbox_signature(conversation)
    print(
        "Meridian watcher started. It exits when a Meridian message is waiting for this "
        "session while you are idle."
    )
    sys.stdout.flush()
    polls = 0
    while True:
        time.sleep(POLL_SECONDS)
        polls += 1
        # The shell ``agy`` started this under is gone: ``agy`` no longer tracks it, so
        # it could wake nobody and would keep the lock.
        if os.getppid() != parent:
            return 0
        if polls % _ALIVE_EVERY_POLLS == 0 and process_birth(agy_pid) != birth:
            return 0
        if busy_path(proc_dir, conversation).exists():
            continue
        signature = _inbox_signature(conversation)
        if signature is None or signature == noted:
            continue
        print(
            "A Meridian message arrived for this session. "
            "It will arrive in this turn: act on it.\n\n"
            f"Then start the watcher again. {start_instructions(agy_pid, conversation)}"
        )
        sys.stdout.flush()
        return 0


__all__ = ["run", "start_instructions", "watch_command", "watcher_running"]
