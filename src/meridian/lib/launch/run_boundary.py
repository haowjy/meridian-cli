"""Persist run exit observations without ever rebinding the entry chat."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.state import session_store, spawn_store


def finalize_run_boundary(
    *, adapter: SubprocessHarness, child_env: dict[str, str], runtime_root: Path,
    spawn_id: str, pid: int | None,
) -> str | None:
    """Return an entry conflict; exit uncertainty is not an execution failure."""
    boundary = adapter.observe_run_boundary(child_env=child_env, pid=pid)
    if boundary is None:
        # TODO(lane-c-trampoline): consume the owned trampoline_successor_id once
        # Lane C provides it through observe_run_boundary; never infer a successor.
        return None
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    if row is None or row.chat_id is None:
        return None
    entry = session_store.get_session_record(runtime_root, row.chat_id)
    if entry is None:
        return None
    observed = boundary.entry_observed
    mismatch = observed is not None and (
        observed.native_store != entry.native_store
        or observed.session_id != entry.harness_session_id
    )
    exit_chat_id = None
    if not mismatch and boundary.exit is not None:
        exit_chat_id = session_store.get_or_create_exit_chat(
            runtime_root, entry.chat_id, str(adapter.id),
            boundary.exit.native_store, boundary.exit.session_id,
        )
    spawn_store.update_spawn(
        runtime_root, spawn_id, entry_chat_id=entry.chat_id,
        exit_chat_id=exit_chat_id,
        exit_identity="mismatch" if mismatch else "verified" if exit_chat_id else "unresolved",
    )
    if mismatch:
        assert observed is not None
        return (
            f"entry_mismatch: assigned ({entry.native_store}, {entry.harness_session_id}), "
            f"observed ({observed.native_store}, {observed.session_id})"
        )
    return None
