"""Start machine hook events, which belong to no project and no session."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import structlog

from meridian.env_registry import HOOK_PAYLOAD_ENV_KEYS, session_free_env
from meridian.lib.hooks.types import hooks_dispatch_enabled
from meridian.lib.platform import get_home_path
from meridian.lib.platform.detach import detached_popen_kwargs
from meridian.lib.platform.python_module import module_argv

logger = structlog.get_logger(__name__)


def launch_inbox_received(message_id: str, message_path: Path) -> None:
    """Start the detached process that fires inbox.received for one message. Never raises.

    Starts nothing when hooks_dispatch_enabled() is false, or when the caller is itself an
    inbox.received row (MERIDIAN_INBOX_MESSAGE_ID is set in its environment): a row that
    files a message would otherwise start rows without end.
    """

    try:
        if not hooks_dispatch_enabled():
            return
        if os.environ.get(HOOK_PAYLOAD_ENV_KEYS["inbox_message_id"]):
            return
        # Nothing of the caller's stays open in the child: a reader of the caller's
        # stdout or stderr gets end of file when the caller exits, not when the rows do.
        subprocess.Popen(
            module_argv("meridian.lib.hooks.machine_event", message_id, str(message_path)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # The caller's directory may be a worktree that is removed while rows run.
            cwd=get_home_path(),
            env=session_free_env(os.environ),
            **detached_popen_kwargs(),
        )
    except Exception as exc:
        logger.warning("inbox_received_launch_failed", message=message_id, error=str(exc))


__all__ = ["launch_inbox_received"]
