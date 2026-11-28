"""A runtime's turn edge: whether the calling primary's harness is mid-turn.

``spawn list`` shows it as the row's ``activity``. Only a running primary of the reporting
harness is written: a child spawn's environment names the child, and a session started
outside Meridian names no row.
"""

from __future__ import annotations

from collections.abc import Mapping

from meridian.lib.core.caller import caller_identity
from meridian.lib.core.spawn_lifecycle import is_active_spawn_status
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.primary_meta import TurnActivity, record_turn_activity


def record_caller_activity(env: Mapping[str, str], *, harness: str, activity: TurnActivity) -> bool:
    """Record ``activity`` on the primary ``env`` names; True when it was written."""

    identity = caller_identity(env)
    if identity is None:
        return False
    row = spawn_store.get_spawn(identity.runtime_root, identity.spawn_id)
    if (
        row is None
        or (row.kind or "").strip() != "primary"
        or row.harness != harness
        or not is_active_spawn_status(row.status)
    ):
        return False
    return record_turn_activity(LaunchPaths.for_row(identity.runtime_root, row), activity)


__all__ = ["record_caller_activity"]
