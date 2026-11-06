"""Presentation for a spawn's native-session run boundary."""

from meridian.lib.core.domain import TERMINAL_SPAWN_STATUSES
from meridian.lib.state.spawn.model import SpawnRecord


def run_boundary_summary(
    row: SpawnRecord,
    *,
    include_unverified: bool = True,
) -> str | None:
    """Show recorded identity evidence, including for inert historical rows."""
    boundary = row.run_boundary
    if boundary is None:
        return None
    entry = f"{row.chat_id or '?'} ({row.harness_session_id or '?'})"
    if boundary.status == "mismatch":
        return f"warning: Session identity mismatch; recorded entry {entry}."
    target = row.current_chat_id or row.chat_id
    if boundary.status == "verified":
        return f"entry {entry} → exit {target}"
    if target != row.chat_id:
        return f"entry {entry} → last observed chat {target}"
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
    target = row.current_chat_id or entry_chat
    status = row.run_boundary.status if row.run_boundary is not None else None
    running = "; run in progress" if row.status not in TERMINAL_SPAWN_STATUSES else ""
    if status == "mismatch":
        return f"{row.id} → {entry_chat} (entry chat; session identity mismatch)"
    if status == "verified":
        return f"{row.id} → {target} (verified exit chat)"
    if target != entry_chat:
        return f"{row.id} → {target} (last observed chat{running})"
    if running:
        return f"{row.id} → {entry_chat} (entry chat{running})"
    if status is None:
        return f"{row.id} → {entry_chat} (entry chat; run predates exit tracking)"
    return f"{row.id} → {entry_chat} (entry chat; exit-session identity unverified)"
