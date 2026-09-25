"""Presentation for a spawn's native-session run boundary."""

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
