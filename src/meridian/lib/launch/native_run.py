"""One native identity pipeline: bind before exec, observe, conclude after teardown."""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
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
from meridian.lib.core.refs import parse_ref
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.launch.artifact_io import LifecycleLog, record_identity_failure
from meridian.lib.launch.session_scope import SessionAttempt
from meridian.lib.state import session_identity, session_store, spawn_store
from meridian.lib.state.native_binding import Conflict
from meridian.lib.state.spawn.model import RunBoundaryOutcome

if TYPE_CHECKING:
    from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
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
    # Last published key: deduplicates repeats. The durable pointer is reread
    # under the publication locks, so no current chat is cached here.
    _current_key: NativeKey | None = None
    _observation_error: NativeIdentityError | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def observation_error(self) -> NativeIdentityError | None:
        """Identity failure from the joined observer, if any."""
        return self._observation_error

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
            if self._current_key is None and establishes_entry:
                if key.native_store != self.entry.native_store:
                    raise NativeEntryMismatch(self.entry, key.fields())
                conflict = self._observe_locked(key.session_id)
                if conflict is not None:
                    raise NativeEntryMismatch(conflict.kept, conflict.attempted)
            # An owned later-session fact can select a chat even if startup was
            # not observed. It cannot fill an unknown original entry in that case.
            if key == self._current_key or self.attempt.spawn_id is None:
                return
            observed_chat_id = session_store.get_or_create_observed_chat(
                self.attempt.runtime_root,
                self.attempt.chat_id,
                key.harness,
                key.native_store,
                key.session_id,
                spawn_id=str(self.attempt.spawn_id),
            )
            assert observed_chat_id is not None  # Only post-exit proof can refuse.
            # The state seam materialized and published under its source locks.
            self._current_key = key
        if observed_chat_id != self.attempt.chat_id:
            try:
                self._warn_overlap(observed_chat_id)
            except Exception:
                # Diagnostics are not publication: the fact stays acknowledged.
                logger.warning("Current-chat overlap diagnostic failed", exc_info=True)

    def _warn_overlap(self, chat_id: str) -> None:
        """Native /resume already happened; record it rather than deny reality."""
        root = self.attempt.runtime_root
        target = session_store.get_session_record(root, chat_id)
        held = target is not None and session_store.is_session_lease_owner_alive(
            root, chat_id, session_instance_id=target.session_instance_id,
        )
        other = session_identity.live_observer_for_chat(
            root, chat_id, records=spawn_store.list_spawns(root).records,
            excluding_spawn_id=str(self.attempt.spawn_id),
        )
        if held or other is not None:
            logger.warning(
                "Observed current chat overlaps live ownership",
                chat_id=chat_id, spawn_id=str(self.attempt.spawn_id),
                other_spawn_id=other or (target.spawn_id if target is not None else None),
            )

    @contextmanager
    def observing(
        self, adapter: SubprocessHarness, *, child_env: Mapping[str, str],
    ) -> Generator[Callable[[int], None], None, None]:
        """Own native observation lexically; yield ``start(pid)`` for on-running.

        No drainer outlives this block, including on exceptions or cancellation,
        and adapters without a reader never start a thread.
        """
        pump: _ObservationPump | None = None

        def start(pid: int) -> None:
            nonlocal pump
            if pump is not None:
                return
            reader = adapter.native_observation_reader(child_env=child_env, pid=pid)
            if reader is not None:
                pump = _ObservationPump(self, reader)

        try:
            yield start
        finally:
            if pump is not None:
                error = pump.close()
                self._observation_error = self._observation_error or error


class _ObservationPump:
    """One run's bounded journal drainer: started when constructed, joined by close."""

    def __init__(self, run: NativeRun, reader: Callable[[int], NativeObservationBatch]) -> None:
        self._run = run
        self._reader = reader
        self._cursor = 0
        self._stop = threading.Event()
        self._error: NativeIdentityError | None = None
        self._thread = threading.Thread(
            target=self._loop,
            name=f"meridian-native-observer-{run.attempt.spawn_id or run.attempt.chat_id}",
            daemon=True,
        )
        self._thread.start()

    def _drain(self) -> bool:
        """Drain one replayable batch. Return whether the cursor advanced."""
        cursor = self._cursor
        batch = self._reader(cursor)
        for observation in batch.observations:
            self._run.apply_observation(observation)
            # Acknowledge each fact after its publication: a later failure must
            # never replay an accepted prefix and briefly rewind the pointer.
            self._cursor = observation.next_cursor
        self._cursor = batch.next_cursor
        return batch.next_cursor != cursor

    def _loop(self) -> None:
        failing = False
        while not self._stop.wait(0.05):
            try:
                self._drain()
            except NativeIdentityError as exc:
                self._error = exc
                return
            except Exception:
                # Leave the cursor unacknowledged and retry. A final failure
                # remains in the spawn journal for diagnostics/replay.
                if not failing:
                    logger.warning("Native observation publication failed", exc_info=True)
                failing = True
            else:
                failing = False
        try:
            while self._drain():
                pass
        except NativeIdentityError as exc:
            self._error = exc
        except Exception:
            logger.warning("Final native observation publication failed", exc_info=True)

    def close(self) -> NativeIdentityError | None:
        """Stop, final-drain and join; no state writes occur after return."""
        self._stop.set()
        self._thread.join()
        return self._error


def bind_entry(
    attempt: SessionAttempt,
    spec: ResolvedLaunchSpec,
    *,
    harness: str,
    policy: LaunchPolicySnapshot | None,
    on_accepted: Callable[[str], None] | None = None,
) -> NativeRun:
    """Final binding: the generation owns its policy and entry before native exec."""
    if policy is not None:
        attempt.own_policy(policy)
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
    """Conclude once per turn, after its child, teardown and observer joined."""
    error = prior_error or run.observation_error
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
                logger.info("Skipping exit chat publication: exact native resolution failed")
                return False

        # A verified exit is one more owned observation: same publication seam,
        # with native proof required under its locks before alias reuse.
        exit_chat_id = session_store.get_or_create_observed_chat(
            run.attempt.runtime_root,
            run.attempt.chat_id,
            exit_key.harness,
            exit_key.native_store,
            exit_key.session_id,
            spawn_id=str(spawn_id),
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
        trampoline_successor_id=post.trampoline_successor_id if exit_chat_id is None else None,
    )
    spawn_store.update_spawn(
        run.attempt.runtime_root, parse_ref(spawn_id, row_key=True), run_boundary=boundary
    )
    if error is not None:
        record_identity_failure(
            error,
            lifecycle=lifecycle,
            phase=prior_error_phase if prior_error is not None else "post_exit",
        )
    elif started:
        run.attempt.record_started(context, run.entry.session_id)
    return NativeRunOutcome(error, boundary, run.entry.session_id)
