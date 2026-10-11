"""Presentation for a spawn's native-session run boundary."""

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.state.spawn.model import SpawnRecord


def run_boundary_summary(
    row: SpawnRecord, *, include_unverified: bool = True,
) -> str | None:
    """Keep absent exit evidence diagnostic; always surface identity conflicts or changes."""
    boundary = row.run_boundary
    if boundary is None:
        return None
    entry = f"{row.chat_id or '?'} ({row.harness_session_id or '?'})"
    if boundary.status == "verified":
        return f"entry {entry} → exit {boundary.exit_chat_id}"
    if boundary.status == "mismatch":
        return f"warning: Session identity mismatch; recorded entry {entry}."
    if boundary.trampoline_successor_id:
        return (
            "warning: Possible session change detected "
            f"({boundary.trampoline_successor_id}); exit-session identity unverified. "
            f"Continue targets recorded entry {entry}."
        )
    if include_unverified:
        return f"Exit-session identity: unverified; recorded entry {entry}."
    return None


def spawn_view_label(row: SpawnRecord) -> str | None:
    entry_chat = row.chat_id or "?"
    if row.status not in TERMINAL_SPAWN_STATUSES:
        return f"{row.id} → {entry_chat} (entry chat; run in progress)"
    boundary = row.run_boundary
    if boundary is None:
        return f"{row.id} → {entry_chat} (entry chat; run predates exit tracking)"
    if boundary.status == "mismatch":
        return f"{row.id} → {entry_chat} (entry chat; session identity mismatch)"
    if boundary.status == "unresolved":
        return f"{row.id} → {entry_chat} (entry chat; exit-session identity unverified)"
    return f"{row.id} → {row.continue_chat_id} (verified exit chat)"
