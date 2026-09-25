"""Shared run-boundary presentation."""

from pathlib import Path

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.state import session_store, spawn_store


def post_run_continue_chat_id(
    *, entry_chat_id: str | None, exit_identity: str | None, exit_chat_id: str | None,
    status: str | None,
) -> str | None:
    """Choose the post-run chat without ever redirecting a chat reference."""
    if status in TERMINAL_SPAWN_STATUSES and exit_identity == "verified" and exit_chat_id:
        return exit_chat_id
    return entry_chat_id


def run_boundary_summary(runtime_root: Path, spawn_id: str) -> str | None:
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    if row is None or row.exit_identity is None:
        return None

    def label(chat_id: str | None) -> str:
        record = session_store.get_session_record(runtime_root, chat_id) if chat_id else None
        return f"{chat_id} ({record.harness_session_id if record else '?'})"

    entry = label(row.entry_chat_id)
    if row.exit_identity == "verified":
        return f"entry {entry} → exit {label(row.exit_chat_id)}"
    suffix = " (entry mismatch)" if row.exit_identity == "mismatch" else ""
    return f"entry {entry} → exit unresolved{suffix}"
