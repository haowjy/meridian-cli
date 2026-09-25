"""Session lifecycle context manager helpers."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from meridian.lib.core.process_cleanup import reclaim_session_owned_scopes_for_chat
from meridian.lib.core.types import ChatId, HarnessSessionId, SpawnId
from meridian.lib.launch.request import SessionRequest, is_exact_continue_session
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import spawn_store
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.session_store import (
    ConversationModelSelection,
    NativeBindingResult,
    SessionModelSelectionEvent,
    get_session_record,
    record_model_selection,
    start_session,
    stop_session,
    update_session_harness_id,
)

if TYPE_CHECKING:
    from meridian.lib.launch.context import LaunchContext

logger = structlog.get_logger(__name__)

SessionIdSource = Literal["assigned", "observed"]


def bind_harness_session_id(
    *,
    runtime_root: Path,
    spawn_id: SpawnId | None,
    record_session_id: Callable[[str], NativeBindingResult | None],
    session_id: str | None,
    source: SessionIdSource,
    chat_id: str | None = None,
    current_session_id: str = "",
) -> str:
    """Bind once and mirror the accepted identity, never the attempted identity."""
    candidate = (session_id or "").strip()
    current = (current_session_id or "").strip()
    if not candidate or candidate == current:
        return current
    if current and candidate != current:
        logger.warning(
            "native_binding_conflict", chat_id=chat_id, kept=current,
            attempted=candidate, source=source,
            spawn_id=str(spawn_id) if spawn_id is not None else None,
        )
        return current
    result = record_session_id(candidate)
    bound = result.harness_session_id if isinstance(result, NativeBindingResult) else candidate
    if spawn_id is not None and bound:
        spawn_store.update_spawn(runtime_root, spawn_id, harness_session_id=bound)
    return bound or ""


@dataclass(frozen=True)
class SessionAttempt:
    """Captured session generation and startup attempt, shared by both callbacks."""

    runtime_root: Path
    chat_id: str
    session_instance_id: str
    startup_attempt_id: str
    native_store: str | None = None

    def record_harness_session_id(self, session_id: str) -> NativeBindingResult:
        return update_session_harness_id(
            self.runtime_root, self.chat_id, session_id, native_store=self.native_store,
            session_instance_id=self.session_instance_id,
            startup_attempt_id=self.startup_attempt_id,
        )

    def record_started(
        self, context: LaunchContext, spawn_id: str, harness_session_id: str | None,
    ) -> None:
        request = context.resolved_request
        snapshot = request.launch_policy_snapshot
        assert snapshot is not None
        executable_model = context.binding.spec.model
        canonical_model = snapshot.model_selection_canonical_id or snapshot.model
        requested_token = snapshot.model_selection_requested_token or canonical_model
        selected_token = snapshot.model_selection_selected_token or canonical_model
        # A resume that preserves the native session (OpenCode attach, dry-run
        # "preserve the existing native session's committed model") carries no
        # model on the launch spec; the executable identity lives in the source
        # snapshot. Fall back to the spec for fresh launches and forks.
        harness_model_id = snapshot.model_selection_harness_model_id or (
            str(executable_model) if executable_model else None
        )
        named = bool(requested_token and selected_token and canonical_model and harness_model_id)
        selection = ConversationModelSelection.model_validate({
            "requested_token": requested_token,
            "selected_token": selected_token,
            "canonical_model_id": canonical_model if named else None,
            "harness_model_id": harness_model_id if named else None,
            "model_mode": "named" if named else "harness_default",
            "provider_constraint": (
                snapshot.model_selection_provider_constraint if named else None
            ),
            "selection_source": (
                request.session.conversation_intent.selection_source
                if is_exact_continue_session(request.session)
                and request.session.conversation_intent is not None
                else "initial_launch"
            ),
            "provenance": snapshot.field_provenance,
        })
        record_model_selection(self.runtime_root, SessionModelSelectionEvent(
            kind="invocation_started",
            harness=str(context.harness.id),
            harness_session_id=(
                HarnessSessionId(harness_session_id) if harness_session_id else None
            ),
            chat_id=ChatId(self.chat_id),
            session_instance_id=self.session_instance_id,
            spawn_id=spawn_id,
            startup_attempt_id=self.startup_attempt_id,
            recorded_at=utc_now_iso(),
            selection=selection,
        ))


@dataclass(frozen=True)
class ManagedSession:
    chat_id: str
    record_harness_session_id: Callable[[str], NativeBindingResult | None]
    attempt: SessionAttempt | None = None


@contextmanager
def session_scope(
    *,
    runtime_root: Path,
    metadata: PrimarySessionMetadata,
    request: SessionRequest,
    harness_session_id: str,
    native_store: str | None = None,
    chat_id: str | None = None,
    params: tuple[str, ...] = (),
    control_root: str | None = None,
    task_cwd: str | None = None,
    execution_cwd: str | None = None,
    kind: Literal["primary", "spawn"] = "spawn",
    spawn_id: str | None = None,
    startup_attempt_id: str | None = None,
    _start_session: Callable[..., str] = start_session,
    _stop_session: Callable[[Path, str], None] = stop_session,
    _update_session_harness_id: Callable[..., NativeBindingResult | None] = (
        update_session_harness_id
    ),
    _reclaim_session_scopes: Callable[[Path, str], object] = reclaim_session_owned_scopes_for_chat,
) -> Generator[ManagedSession, None, None]:
    if request.initial_model_selection is not None:
        record_model_selection(runtime_root, request.initial_model_selection)
    resolved_chat_id = _start_session(
        runtime_root,
        harness=metadata.harness,
        harness_session_id=harness_session_id,
        native_store=native_store,
        model=metadata.model,
        chat_id=chat_id,
        params=params,
        agent=metadata.agent,
        agent_path=metadata.agent_path,
        skills=metadata.skills,
        skill_paths=metadata.skill_paths,
        forked_from_chat_id=request.forked_from_chat_id,
        forked_from_history_id=request.forked_from_history_id,
        control_root=control_root,
        task_cwd=task_cwd,
        execution_cwd=execution_cwd,
        kind=kind,
        spawn_id=spawn_id,
        model_selection_protocol=1 if startup_attempt_id is not None else None,
    )
    record = get_session_record(runtime_root, resolved_chat_id)
    generation = record.session_instance_id if record is not None else ""
    attempt = (
        SessionAttempt(runtime_root, resolved_chat_id, generation, startup_attempt_id)
        if startup_attempt_id is not None else None
    )

    def _record_harness_session_id(session_id: str) -> NativeBindingResult | None:
        if startup_attempt_id is None:
            return _update_session_harness_id(runtime_root, resolved_chat_id, session_id)
        else:
            return _update_session_harness_id(
                runtime_root, resolved_chat_id, session_id,
                session_instance_id=generation, startup_attempt_id=startup_attempt_id,
            )

    try:
        yield ManagedSession(
            chat_id=resolved_chat_id,
            record_harness_session_id=_record_harness_session_id,
            attempt=attempt,
        )
    finally:
        try:
            _stop_session(runtime_root, resolved_chat_id)
        finally:
            _reclaim_session_scopes(runtime_root, resolved_chat_id)


__all__ = ["ManagedSession", "SessionAttempt", "bind_harness_session_id", "session_scope"]
