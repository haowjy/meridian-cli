"""Presentation for a spawn's native-session run boundary."""

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.state.spawn.model import SpawnRecord


def run_boundary_summary(row: SpawnRecord) -> str | None:
    boundary = row.run_boundary
    if boundary is None:
        return None
    entry = f"{row.chat_id or '?'} ({row.harness_session_id or '?'})"
    if boundary.status == "verified":
        return f"entry {entry} → exit {boundary.exit_chat_id}"
    suffix = " (entry mismatch)" if boundary.status == "mismatch" else ""
    return f"entry {entry} → exit unresolved{suffix}"


def spawn_view_label(row: SpawnRecord) -> str | None:
    entry_chat = row.chat_id or "?"
    if row.status not in TERMINAL_SPAWN_STATUSES:
        return f"{row.id} → {entry_chat} (entry chat; run in progress)"
    boundary = row.run_boundary
    if boundary is None:
        return f"{row.id} → {entry_chat} (entry chat; run predates exit tracking)"
    if boundary.status != "verified":
        return f"{row.id} → {entry_chat} (entry chat; exit identity {boundary.status})"
    return f"{row.id} → {row.continue_chat_id} (verified exit chat)"
