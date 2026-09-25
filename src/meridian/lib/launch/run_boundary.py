"""Persist run exit observations without ever rebinding the entry chat."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import structlog

from meridian.lib.core.native_identity import (
    NativeEntryMismatch,
    NativeIdentityError,
    NativeKeyFields,
    RunBoundary,
)
from meridian.lib.core.types import OptionalPersistedChatId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.spawn.model import RunBoundaryOutcome

logger = structlog.get_logger()


def finalize_run_boundary(
    *, adapter: SubprocessHarness, child_env: dict[str, str], runtime_root: Path,
    spawn_id: str, pid: int | None,
    identity_error: NativeIdentityError | None = None,
) -> NativeIdentityError | None:
    """Return an entry conflict; exit uncertainty is not an execution failure."""
    boundary = (
        adapter.observe_run_boundary(child_env=child_env, pid=pid)
        if identity_error is None else None
    )
    if boundary is None:
        boundary = RunBoundary()
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    if row is None or row.chat_id is None:
        return identity_error
    entry = session_store.get_session_record(runtime_root, row.chat_id)
    if entry is None:
        return identity_error
    observed = boundary.entry_observed
    mismatch = observed is not None and (
        observed.native_store != entry.native_store
        or observed.session_id != entry.harness_session_id
    )
    if mismatch:
        assert observed is not None
        identity_error = NativeEntryMismatch(
            NativeKeyFields(entry.harness, entry.native_store, entry.harness_session_id),
            NativeKeyFields(str(adapter.id), observed.native_store, observed.session_id),
        )
    exit_chat_id = None
    if identity_error is None and boundary.exit is not None:
        exit_key = boundary.exit

        def native_exists() -> bool:
            try:
                source = adapter.resolve_native_session_file(
                    session_id=exit_key.session_id,
                    native_store=Path(exit_key.native_store),
                )
                return source is not None and source.is_file()
            except Exception:
                logger.info(
                    "skipping exit chat allocation: exact native session resolution failed",
                    harness=str(adapter.id), native_store=exit_key.native_store,
                    session_id=exit_key.session_id,
                )
                return False

        exit_chat_id = session_store.get_or_create_exit_chat(
            runtime_root, entry.chat_id, str(adapter.id), exit_key.native_store,
            exit_key.session_id, native_exists=native_exists,
        )
        if exit_chat_id is None:
            logger.info(
                "skipping exit chat allocation: native session does not exist",
                harness=str(adapter.id), native_store=exit_key.native_store,
                session_id=exit_key.session_id,
            )
    spawn_store.update_spawn(
        runtime_root, spawn_id,
        run_boundary=RunBoundaryOutcome(
            status=("mismatch" if isinstance(identity_error, NativeEntryMismatch)
                    else "verified" if exit_chat_id else "unresolved"),
            exit_chat_id=cast("OptionalPersistedChatId | None", exit_chat_id),
        ),
    )
    return identity_error
