"""Spawn session scope helpers: session continuation and session context manager."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.types import HarnessId
from meridian.lib.launch.request import SessionRequest, SpawnRequest
from meridian.lib.launch.session_scope import session_scope
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state.session_store import get_session_active_work_id, update_session_work_id

from .execute_init import LaunchUserInputError


class _SessionExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    chat_id: str
    work_id: str | None = None
    resolved_agent_name: str | None
    harness_session_id_observer: Callable[[str], None]


def _resolve_session_continuation(
    *,
    request: SpawnRequest,
    harness_id: HarnessId,
    harness_adapter: object,
) -> SessionRequest:
    from typing import Any

    adapter: Any = harness_adapter
    requested_harness_session_id = (
        request.session.requested_harness_session_id or ""
    ).strip() or None
    requested_continue_fork = request.session.continue_fork
    requested_harness = (request.session.continue_harness or "").strip()
    if request.session.continue_source_tracked and requested_harness_session_id is None:
        raise LaunchUserInputError(
            "Source reference has no recorded harness session — cannot continue/fork."
        )

    resolved_continue_harness_session_id: str | None = None
    resolved_continue_fork = False
    if requested_harness_session_id:
        if (
            requested_harness and requested_harness != str(harness_id)
        ) or not adapter.capabilities.supports_session_resume:
            resolved_continue_harness_session_id = None
        else:
            resolved_continue_harness_session_id = requested_harness_session_id
            if requested_continue_fork:
                if adapter.capabilities.supports_session_fork:
                    resolved_continue_fork = True
                else:
                    resolved_continue_fork = False

    # I-10: fork materialization is deferred to the calling executor, which
    # calls materialize_fork() via the sole owner in launch/fork.py, after both
    # the spawn row and chat row exist.  resolved_continue_fork=True is preserved
    # here so the executor knows a fork is needed.

    return SessionRequest(
        requested_harness_session_id=resolved_continue_harness_session_id,
        continue_harness=request.session.continue_harness,
        continue_source_tracked=request.session.continue_source_tracked,
        continue_source_ref=request.session.continue_source_ref,
        continue_chat_id=request.session.continue_chat_id,
        continue_fork=resolved_continue_fork,
        forked_from_chat_id=request.session.forked_from_chat_id,
        source_control_root=request.session.source_control_root,
        source_execution_cwd=request.session.source_execution_cwd,
        source_claude_config_dir=request.session.source_claude_config_dir,
        source_pi_session_dir=request.session.source_pi_session_dir,
    )


@contextmanager
def _session_execution_context(
    *,
    runtime_root: Path,
    metadata: PrimarySessionMetadata,
    request: SessionRequest,
    harness_session_id: str,
    run_agent_name: str | None,
    inherited_work_id: str | None = None,
    control_root: str | None = None,
    task_cwd: str | None = None,
    execution_cwd: str | None = None,
) -> Generator[_SessionExecutionContext, None, None]:
    with session_scope(
        runtime_root=runtime_root,
        metadata=metadata,
        request=request,
        harness_session_id=harness_session_id,
        control_root=control_root,
        task_cwd=task_cwd,
        execution_cwd=execution_cwd,
    ) as managed:
        attached_work_id = get_session_active_work_id(runtime_root, managed.chat_id)
        if attached_work_id is None:
            attached_work_id = (inherited_work_id or "").strip() or None
            if attached_work_id is not None:
                update_session_work_id(runtime_root, managed.chat_id, attached_work_id)
        yield _SessionExecutionContext(
            chat_id=managed.chat_id,
            work_id=attached_work_id,
            resolved_agent_name=run_agent_name,
            harness_session_id_observer=managed.record_harness_session_id,
        )


__all__ = [
    "_SessionExecutionContext",
    "_resolve_session_continuation",
    "_session_execution_context",
]
