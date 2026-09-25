"""One native identity pipeline: bind before exec, observe, conclude after teardown."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from meridian.lib.core.native_identity import (
    NativeEntryMismatch,
    NativeIdentity,
    NativeIdentityError,
    NativeKeyFields,
    PostExit,
)
from meridian.lib.core.types import ChatId, SpawnId
from meridian.lib.launch.artifact_io import LifecycleLog, record_identity_failure
from meridian.lib.launch.session_scope import SessionAttempt
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.native_binding import Conflict
from meridian.lib.state.spawn.model import RunBoundaryOutcome

if TYPE_CHECKING:
    from meridian.lib.harness.adapter import SubprocessHarness
    from meridian.lib.launch.context import LaunchContext
    from meridian.lib.launch.launch_types import ResolvedLaunchSpec
    from meridian.lib.state.artifact_store import ArtifactStore

logger = structlog.get_logger(__name__)


@dataclass
class NativeRun:
    attempt: SessionAttempt
    identity: NativeIdentity | None
    entry: NativeKeyFields
    assigned_session_id: str | None
    fork_source_id: str | None
    on_accepted: Callable[[str], None] | None = None
    _first_seen: bool = False
    _noted: set[str] = field(default_factory=set)

    def observe(self, session_id: str) -> None:
        """Only first owned signals can contradict pre-exec facts."""
        candidate = session_id.strip()
        if not candidate:
            return
        if not self._first_seen:
            if self.assigned_session_id and candidate != self.assigned_session_id:
                raise NativeEntryMismatch(self.entry, self.entry.with_session(candidate))
            if self.fork_source_id and candidate == self.fork_source_id:
                raise NativeEntryMismatch(
                    NativeKeyFields(self.entry.harness, self.entry.native_store),
                    self.entry.with_session(candidate),
                    reason="fork_reused_source",
                )
            self._first_seen = True
        self.note(candidate)

    def note(self, session_id: str) -> None:
        """Current transport IDs are diagnostic, never startup confirmation."""
        candidate = session_id.strip()
        if not candidate:
            return
        # Live callbacks, on-running, artifact extraction and current transport
        # state can repeat the same signal. Bind/log each candidate once per attempt.
        if candidate in self._noted:
            return
        self._noted.add(candidate)
        outcome = self.attempt.bind(self.entry.with_session(candidate), "observed")
        if not isinstance(outcome, Conflict):
            self.entry = outcome.key
            if self.on_accepted is not None:
                self.on_accepted(candidate)

    def retry(self, attempt: SessionAttempt) -> NativeRun:
        return replace(self, attempt=attempt, _first_seen=False, _noted=set())


def bind_entry(
    attempt: SessionAttempt,
    spec: ResolvedLaunchSpec,
    *,
    harness: str,
    on_accepted: Callable[[str], None] | None = None,
) -> NativeRun:
    identity = spec.native_identity
    if identity is None:
        assigned = spec.continue_session_id if not spec.continue_fork else None
        fork_source = spec.continue_session_id if spec.continue_fork else None
        entry = NativeKeyFields(harness, session_id=assigned)
    else:
        assigned = identity.session_id
        fork_source = None
        if identity.operation == "fork" and assigned is None:
            fork_source = identity.source_session_id
        entry = identity.entry_fields()
    if assigned:
        outcome = attempt.bind(entry, "assigned")
        if isinstance(outcome, Conflict):
            raise NativeEntryMismatch(outcome.kept, outcome.attempted)
        entry = outcome.key
        if on_accepted is not None:
            on_accepted(assigned)
    return NativeRun(attempt, identity, entry, assigned, fork_source, on_accepted)


@dataclass(frozen=True)
class NativeRunOutcome:
    error: NativeIdentityError | None
    boundary: RunBoundaryOutcome
    harness_session_id: str | None


def conclude_native_run(
    run: NativeRun,
    adapter: SubprocessHarness,
    *,
    context: LaunchContext,
    spawn_id: SpawnId,
    child_env: Mapping[str, str],
    child_cwd: Path,
    pid: int | None,
    started: bool,
    started_at_epoch: float | None,
    prior_error: NativeIdentityError | None,
    artifacts: ArtifactStore | None,
    connection_session_id: str | None,
    lifecycle: LifecycleLog,
    prior_error_phase: str = "post_exit",
) -> NativeRunOutcome:
    """Conclude once per attempt, after its child exited and teardown joined."""
    error = prior_error
    post = PostExit()
    if error is None:
        extracted = None
        if artifacts is not None:
            try:
                extracted = adapter.extract_session_id(artifacts, spawn_id)
            except Exception:
                logger.debug("Best-effort harness session observation failed", exc_info=True)
        try:
            if extracted:
                run.observe(extracted)
            if connection_session_id:
                run.note(connection_session_id)
            if started and run.identity is not None:
                post = adapter.observe_after_exit(
                    run.identity,
                    run.entry,
                    child_env=child_env,
                    child_cwd=child_cwd,
                    pid=pid,
                    started_at_epoch=started_at_epoch,
                )
            error = post.entry_error
            if error is None and post.entry_observed is not None:
                observed = post.entry_observed.fields()
                if observed != run.entry:
                    error = NativeEntryMismatch(run.entry, observed)
        except NativeIdentityError as exc:
            error = exc

    exit_chat_id = None
    if error is None and post.exit is not None:
        exit_key = post.exit

        def native_exists() -> bool:
            try:
                source = adapter.resolve_native_session_file(
                    session_id=exit_key.session_id,
                    native_store=Path(exit_key.native_store),
                )
                return source is not None and source.is_file()
            except (NativeIdentityError, OSError):
                logger.info("Skipping exit chat allocation: exact native resolution failed")
                return False

        exit_chat_id = session_store.get_or_create_exit_chat(
            run.attempt.runtime_root,
            run.attempt.chat_id,
            exit_key.harness,
            exit_key.native_store,
            exit_key.session_id,
            native_exists=native_exists,
        )
    if isinstance(error, NativeEntryMismatch):
        status = "mismatch"
    elif exit_chat_id:
        status = "verified"
    else:
        status = "unresolved"
    boundary = RunBoundaryOutcome(
        status=status,
        exit_chat_id=ChatId(exit_chat_id) if exit_chat_id else None,
        trampoline_successor_id=post.trampoline_successor_id,
    )
    spawn_store.update_spawn(run.attempt.runtime_root, spawn_id, run_boundary=boundary)
    if error is not None:
        record_identity_failure(
            error,
            lifecycle=lifecycle,
            phase=prior_error_phase if prior_error is not None else "post_exit",
        )
    elif started:
        run.attempt.record_started(context, run.entry.session_id)
    return NativeRunOutcome(error, boundary, run.entry.session_id)
