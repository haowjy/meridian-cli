"""A spawn's open idle stretch: what `spawn list --json` shows and `meridian message` reads."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.idle.deadline import cache_deadline
from meridian.lib.idle.delivery import end_stretch_on_delivery
from meridian.lib.ops.spawn.models import SpawnIdle
from meridian.lib.state.idle_store import IdleStore
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.primary_meta import read_primary_surface_metadata
from meridian.lib.state.spawn.model import SpawnRecord


def iso_utc_ms(epoch_ms: int) -> str:
    return (
        datetime.fromtimestamp(epoch_ms / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def spawn_idle(row: SpawnRecord, session_id: str | None, activity: str | None) -> SpawnIdle | None:
    """The row's open idle stretch: one file read keyed by its own harness session.

    A stretch closes when a user-loop turn starts, so injected turns (wakes, task
    notifications, peer messages, keepwarm) leave it open. A primary whose harness
    reports it mid-turn is not idle, whatever a crashed session's stretch says.
    """

    if (
        not session_id
        or not row.harness
        or not is_active_spawn_status(row.status)
        or activity in {"starting", "turn_active", "finalizing"}
    ):
        return None
    try:
        state = IdleStore().read(row.harness, session_id)
    except ValueError:  # a session id the store refuses as a filename
        return None
    if state is None or not state.stretch_open or state.idle_since_ms is None:
        return None
    deadline = cache_deadline(row.harness, state.idle_since_ms / 1000, state)
    return SpawnIdle(
        since=iso_utc_ms(state.idle_since_ms),
        warm_until=iso_utc_ms(round(deadline[0] * 1000)) if deadline else None,
        ttl_estimated=deadline[1] if deadline else True,
    )


def row_session(runtime_root: Path, row: SpawnRecord) -> tuple[str | None, str | None]:
    """The harness session of a row and its reported activity; a primary's come from its
    metadata, as ``spawn list`` reads them."""

    session_id = row.harness_session_id
    activity: str | None = None
    if (row.kind or "").strip() == "primary":
        metadata = read_primary_surface_metadata(LaunchPaths.for_row(runtime_root, row))
        activity = metadata.activity
        session_id = metadata.harness_session_id or session_id
    return session_id, activity


def row_idle(runtime_root: Path, row: SpawnRecord) -> SpawnIdle | None:
    """``spawn_idle`` for a row."""

    session_id, activity = row_session(runtime_root, row)
    return spawn_idle(row, session_id, activity)


def end_row_idle_stretch(runtime_root: Path, row: SpawnRecord) -> None:
    """A message was delivered to ``row``: its open idle stretch is over."""

    session_id, _ = row_session(runtime_root, row)
    if session_id and row.harness:
        end_stretch_on_delivery(row.harness, session_id)
