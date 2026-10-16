"""Session lifecycle context manager helpers."""

from __future__ import annotations

import importlib
import logging
import pkgutil
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from meridian.lib.core.native_identity import BindSource, NativeKeyFields
from meridian.lib.core.process_cleanup import reclaim_session_owned_scopes_for_chat
from meridian.lib.core.types import ChatId, HarnessSessionId, SpawnId
from meridian.lib.launch.request import SessionRequest, is_exact_continue_session
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import spawn_store
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.native_binding import BindOutcome, Conflict
from meridian.lib.state.session_store import (
    ConversationModelSelection,
    SessionModelSelectionEvent,
    SessionRecord,
    StartedSession,
    record_model_selection,
    start_session,
    stop_session,
    update_session_harness_id,
    update_session_launch_policy,
)

if TYPE_CHECKING:
    from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
    from meridian.lib.launch.context import LaunchContext


# Outside the walked packages, but part of capture's lazy close-time closure.
_CLOSE_PATH_MODULES = (
    "meridian.lib.harness.capture_qualify",
    "meridian.lib.harness.transcript_capture",
)


def warm_close_imports() -> threading.Thread:
    """Pin close-path code before an in-place package upgrade can replace it."""

    def load() -> None:
        with suppress(Exception):
            try:
                for name in ("meridian.lib.state", "meridian.lib.ops"):
                    package = importlib.import_module(name)
                    for module in pkgutil.walk_packages(package.__path__, name + "."):
                        importlib.import_module(module.name)
                for name in _CLOSE_PATH_MODULES:
                    importlib.import_module(name)
            except Exception:
                logging.getLogger(__name__).debug("Close-path warm import failed", exc_info=True)
                raise

    thread = threading.Thread(target=load, name="meridian-close-imports", daemon=True)
    thread.start()
    return thread


@dataclass(frozen=True)
class SessionAttempt:
    """Captured session generation and startup attempt, shared by both callbacks."""

    runtime_root: Path
    chat_id: str
    session_instance_id: str
    startup_attempt_id: str | None
    record: SessionRecord
    spawn_id: SpawnId | None = None

    def bind(self, attempted: NativeKeyFields, source: BindSource) -> BindOutcome:
        outcome = update_session_harness_id(
            self.runtime_root,
            self.chat_id,
            attempted,
            source=source,
            session_instance_id=self.session_instance_id,
            startup_attempt_id=self.startup_attempt_id,
        )
        if (
            not isinstance(outcome, Conflict)
            and self.spawn_id is not None
            and outcome.key.session_id
        ):
            spawn_store.update_spawn(
                self.runtime_root,
                self.spawn_id,
                harness_session_id=outcome.key.session_id,
            )
        return outcome

    def own_policy(self, snapshot: LaunchPolicySnapshot) -> None:
        """Persist the final launch policy on this generation before native execution."""
        update_session_launch_policy(
            self.runtime_root,
            self.chat_id,
            self.session_instance_id,
            snapshot,
        )

    def record_started(
        self,
        context: LaunchContext,
        harness_session_id: str | None,
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
        selection = ConversationModelSelection.model_validate(
            {
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
            }
        )
        record_model_selection(
            self.runtime_root,
            SessionModelSelectionEvent(
                kind="invocation_started",
                harness=str(context.harness.id),
                harness_session_id=(
                    HarnessSessionId(harness_session_id) if harness_session_id else None
                ),
                chat_id=ChatId(self.chat_id),
                session_instance_id=self.session_instance_id,
                spawn_id=self.spawn_id,
                startup_attempt_id=self.startup_attempt_id,
                recorded_at=utc_now_iso(),
                selection=selection,
            ),
        )


@contextmanager
def session_scope(
    *,
    runtime_root: Path,
    metadata: PrimarySessionMetadata,
    request: SessionRequest,
    chat_id: str | None = None,
    params: tuple[str, ...] = (),
    control_root: str | None = None,
    task_cwd: str | None = None,
    execution_cwd: str | None = None,
    kind: Literal["primary", "spawn"] = "spawn",
    spawn_id: str | None = None,
    startup_attempt_id: str | None = None,
    _start_session: Callable[..., StartedSession] = start_session,
    _stop_session: Callable[[Path, str], None] = stop_session,
    _reclaim_session_scopes: Callable[[Path, str], object] = reclaim_session_owned_scopes_for_chat,
) -> Generator[SessionAttempt, None, None]:
    warm_close_imports()
    if request.initial_model_selection is not None:
        record_model_selection(runtime_root, request.initial_model_selection)
    started = _start_session(
        runtime_root,
        harness=metadata.harness,
        harness_session_id="",
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
    resolved_chat_id = started.chat_id
    generation = started.generation
    try:
        yield SessionAttempt(
            runtime_root,
            resolved_chat_id,
            generation,
            startup_attempt_id,
            record=started.record,
            spawn_id=SpawnId(spawn_id) if spawn_id is not None else None,
        )
    finally:
        try:
            _stop_session(runtime_root, resolved_chat_id)
        finally:
            _reclaim_session_scopes(runtime_root, resolved_chat_id)


__all__ = ["SessionAttempt", "session_scope"]
