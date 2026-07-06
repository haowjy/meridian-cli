"""Fork materialization — sole owner of the adapter.fork_session callsite.

Invariant I-3 (Single owners): materialize_fork() is the only callsite that
invokes adapter.fork_session().  No other module may call fork_session()
directly.

Invariant I-10 (Fork-after-row ordering): fork_session is invoked only after
a spawn row exists for the current launch.  Callers MUST call start_spawn
before materialize_fork().

Invariant I-11 (Fork lineage coherence): the spawn row's harness_session_id
is written via update_spawn after forking — the start row MUST NOT
pre-populate it.  materialize_fork() owns that update_spawn call.
"""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.state import spawn_store


def materialize_fork(
    *,
    adapter: SubprocessHarness,
    source_session_id: str,
    runtime_root: Path,
    spawn_id: SpawnId,
) -> str:
    """Fork one harness session and record the new session ID on the spawn row.

    Precondition: a spawn row for *spawn_id* must already exist in the spawn
    store (``spawns/<id>/state.json``).  Raises ``RuntimeError`` if the row is absent.

    Returns the new (forked) harness session ID.
    """

    row = spawn_store.get_spawn(runtime_root, spawn_id)
    if row is None:
        raise RuntimeError(
            f"Fork precondition violated: no spawn row exists for {spawn_id!r}. "
            "Call start_spawn() before materialize_fork()."
        )

    forked_session_id = adapter.fork_session(source_session_id).strip()
    if not forked_session_id:
        raise RuntimeError("Harness adapter.fork_session() returned an empty session ID.")

    spawn_store.update_spawn(
        runtime_root,
        spawn_id,
        harness_session_id=forked_session_id,
    )
    return forked_session_id


__all__ = ["materialize_fork"]
