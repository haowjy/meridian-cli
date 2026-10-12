"""One native identity pipeline: bind before exec, observe, conclude after teardown."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from meridian.lib.core.native_identity import (
    NativeEntryMismatch,
    NativeIdentity,
    NativeIdentityError,
    NativeKey,
    NativeKeyFields,
    NativeObservation,
    NativeObservationBatch,
    PostExit,
)
from meridian.lib.core.types import ChatId, SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.launch.artifact_io import LifecycleLog, record_identity_failure
from meridian.lib.launch.session_scope import SessionAttempt
from meridian.lib.state import session_identity, session_store, spawn_store
from meridian.lib.state.native_binding import Conflict
from meridian.lib.state.spawn.model import RunBoundaryOutcome

if TYPE_CHECKING:
    from meridian.lib.harness.adapter import SubprocessHarness
    from meridian.lib.launch.context import LaunchContext
    from meridian.lib.launch.launch_types import ResolvedLaunchSpec

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
    _noted: dict[str, Conflict | None] = field(
        default_factory=lambda: dict[str, Conflict | None](),
    )
    _current_chat_id: str = ""
    _current_key: NativeKey | None = None
    _hook_started: bool = False
    _observation_cursor: int = 0
    _observation_reader: Callable[[int], NativeObservationBatch] | None = None
    _observation_thread: threading.Thread | None = None
    _observation_stop: threading.Event = field(default_factory=threading.Event)
    _observation_identity_error: NativeIdentityError | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def observe(self, session_id: str) -> None:
        """Only first owned signals can contradict pre-exec facts."""
        with self._lock:
            self._observe_locked(session_id)

    def _observe_locked(self, session_id: str) -> Conflict | None:
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
        conflict = self._note_locked(candidate)
        if conflict is not None and not self._first_seen:
            raise NativeEntryMismatch(conflict.kept, conflict.attempted)
        self._first_seen = True
        return conflict

    def note(self, session_id: str) -> None:
        """Current transport IDs are diagnostic, never startup confirmation."""
        with self._lock:
            self._note_locked(session_id)

    def _note_locked(self, session_id: str) -> Conflict | None:
        candidate = session_id.strip()
        if not candidate:
            return
        # Live callbacks, on-running, attempt facts and current transport
        # state can repeat the same signal. Bind/log each candidate once per turn.
        if candidate in self._noted:
            return self._noted[candidate]
        outcome = self.attempt.bind(self.entry.with_session(candidate), "observed")
        if isinstance(outcome, Conflict):
            self._noted[candidate] = outcome
            return outcome
        self.entry = outcome.key
        if self.on_accepted is not None:
            self.on_accepted(candidate)
        self._noted[candidate] = None
        return None

    def apply_observation(self, observation: NativeObservation) -> None:
        """Publish one ordered hook fact without mutating the immutable entry binding."""

        with self._lock:
            key = observation.key
            if key.harness != self.entry.harness:
                raise NativeEntryMismatch(self.entry, key.fields())
            planned_operation = self.identity.operation if self.identity is not None else None
            establishes_entry = observation.entry_operation == "create" or (
                observation.entry_operation is not None
                and observation.entry_operation == planned_operation
                and self.assigned_session_id in {None, key.session_id}
            )
            if not self._hook_started and establishes_entry:
                if key.native_store != self.entry.native_store:
                    raise NativeEntryMismatch(self.entry, key.fields())
                conflict = self._observe_locked(key.session_id)
                if conflict is not None:
                    raise NativeEntryMismatch(conflict.kept, conflict.attempted)
            # An owned later-session fact can select a chat even if startup was
            # not observed. It cannot fill an unknown original entry in that case.
            if key == self._current_key:
                return
            if self.attempt.spawn_id is None:
                return
            source_chat_id = self._current_chat_id or self.attempt.chat_id
            observed_chat_id = session_store.get_or_create_observed_chat(
                self.attempt.runtime_root,
                source_chat_id,
                key.harness,
                key.native_store,
                key.session_id,
                spawn_id=str(self.attempt.spawn_id),
            )
            # The state seam materializes and publishes atomically under its
            # source locks. Only then may memory and the journal cursor advance.
            self._current_chat_id = observed_chat_id
            self._current_key = key
            self._hook_started = True
            if observed_chat_id != self.attempt.chat_id:
                # Native /resume already happened; record it rather than deny
                # reality. Warn about known overlap without claiming exclusivity.
                root = self.attempt.runtime_root
                target = session_store.get_session_record(root, observed_chat_id)
                held = target is not None and session_store.is_session_lease_owner_alive(
                    root, observed_chat_id, session_instance_id=target.session_instance_id,
                )
                other = session_identity.live_observer_for_chat(
                    root, observed_chat_id, records=spawn_store.list_spawns(root).records,
                    excluding_spawn_id=str(self.attempt.spawn_id),
                )
                if held or other is not None:
                    logger.warning(
                        "Observed current chat overlaps live ownership",
                        chat_id=observed_chat_id, spawn_id=str(self.attempt.spawn_id),
                        other_spawn_id=other or (target.spawn_id if target is not None else None),
                    )

    def drain_observations(self) -> bool:
        """Drain one replayable batch. Return whether the cursor advanced."""

        reader = self._observation_reader
        if reader is None:
            return False
        cursor = self._observation_cursor
        batch = reader(cursor)
        for observation in batch.observations:
            self.apply_observation(observation)
            # A later failure must never replay already-published earlier
            # switches and briefly rewind the current-chat pointer.
            self._observation_cursor = observation.next_cursor
        self._observation_cursor = batch.next_cursor
        return batch.next_cursor != cursor

    def start_observing(
        self,
        adapter: SubprocessHarness,
        *,
        child_env: Mapping[str, str],
        pid: int,
    ) -> None:
        """Start one bounded-lifetime journal drainer for this native run."""

        if self._observation_thread is not None:
            return
        self._observation_reader = adapter.native_observation_reader(child_env=child_env, pid=pid)
        if self._observation_reader is None:
            return
        self._observation_stop.clear()

        def _drain() -> None:
            publication_failed = False
            while not self._observation_stop.wait(0.05):
                try:
                    self.drain_observations()
                except NativeIdentityError as exc:
                    self._observation_identity_error = exc
                    return
                except Exception:
                    # Leave the cursor unacknowledged and retry. A final failure
                    # remains in the spawn journal for diagnostics/replay.
                    if not publication_failed:
                        logger.warning("Native observation publication failed", exc_info=True)
                    publication_failed = True
                else:
                    publication_failed = False
            try:
                while self.drain_observations():
                    pass
            except NativeIdentityError as exc:
                self._observation_identity_error = exc
            except Exception:
                logger.warning("Final native observation publication failed", exc_info=True)

        self._observation_thread = threading.Thread(
            target=_drain,
            name=f"meridian-native-observer-{self.attempt.spawn_id or self.attempt.chat_id}",
            daemon=True,
        )
        self._observation_thread.start()

    def stop_observing(self) -> NativeIdentityError | None:
        """Stop and join the per-run drainer; no state writes occur after return."""

        thread = self._observation_thread
        if thread is None:
            return self._observation_identity_error
        self._observation_stop.set()
        thread.join()
        self._observation_thread = None
        return self._observation_identity_error


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
    return NativeRun(
        attempt,
        identity,
        entry,
        assigned,
        fork_source,
        on_accepted,
        _current_chat_id=attempt.chat_id,
    )


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
    facts: AttemptFacts,
    connection_session_id: str | None,
    lifecycle: LifecycleLog,
    prior_error_phase: str = "post_exit",
) -> NativeRunOutcome:
    """Conclude once per turn, after its child exited and teardown joined."""
    observation_error = run.stop_observing()
    error = prior_error or observation_error
    post = PostExit()
    if error is None:
        try:
            if facts.first_session_id:
                run.observe(facts.first_session_id)
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
                if (run.entry.session_id is None
                        and observed.harness == run.entry.harness
                        and observed.native_store == run.entry.native_store):
                    # A native fork may allocate its ID inside the harness. An owned
                    # startup receipt can complete that entry, never change its namespace.
                    run.observe(post.entry_observed.session_id)
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
        trampoline_successor_id=post.trampoline_successor_id if exit_chat_id is None else None,
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
