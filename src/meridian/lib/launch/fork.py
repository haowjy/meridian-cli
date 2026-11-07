from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.launch.request import SessionRequest
from meridian.lib.state import spawn_store
from meridian.lib.state.session_identity import chat_owner_spawn

if TYPE_CHECKING:
    from pydantic import JsonValue


def fork_usage_seed(runtime_root: Path, session: SessionRequest) -> dict[str, JsonValue] | None:
    # Claude and Codex fork usage includes the source session's cumulative usage.
    source_session_id = session.requested_harness_session_id
    if not session.continue_fork or not session.forked_from_chat_id or not source_session_id:
        return None
    owner_id = chat_owner_spawn(runtime_root, session.forked_from_chat_id)
    source = spawn_store.get_spawn(runtime_root, str(owner_id)) if owner_id is not None else None
    if source is None or source.harness_session_id != source_session_id:
        return None
    terminal_total = source.terminal.harness_cumulative if source.terminal is not None else None
    return terminal_total or source.prior_usage.harness_cumulative


def materialize_fork(
    *,
    adapter: SubprocessHarness,
    source_session_id: str,
    runtime_root: Path,
    spawn_id: SpawnId,
    native_store: str | None = None,
) -> str:
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    if row is None:
        raise RuntimeError(
            f"Fork precondition violated: no spawn row exists for {spawn_id!r}. "
            "Call start_spawn() before materialize_fork()."
        )

    forked_session_id = (
        adapter.fork_session(source_session_id, native_store=native_store)
        if native_store is not None
        else adapter.fork_session(source_session_id)
    ).strip()
    if not forked_session_id:
        raise RuntimeError("Harness adapter.fork_session() returned an empty session ID.")

    spawn_store.update_spawn(
        runtime_root,
        spawn_id,
        harness_session_id=forked_session_id,
    )
    return forked_session_id


__all__ = ["fork_usage_seed", "materialize_fork"]
