"""Machine events: hook events that belong to the machine and to no project."""

from pathlib import Path


def launch_inbox_received(message_id: str, message_path: Path) -> None:
    """Start the detached process that fires inbox.received for one message. Never raises.

    Starts nothing when hooks_dispatch_enabled() is false, or when the caller is itself an
    inbox.received row.
    """

    # Stub: the ``inbox-hooks`` branch replaces this module at the merge.
    _ = (message_id, message_path)


__all__ = ["launch_inbox_received"]
