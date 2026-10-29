"""Authoritative lifecycle-transition seam for spawn state.

This module is the single service boundary for lifecycle transitions
(``start``, ``record_launch_facts``, ``finalize``) and post-write
lifecycle hook dispatch.
Persistence remains delegated to :mod:`meridian.lib.state.spawn_store`.

Design decisions in effect:
- D1: Service wraps store, not replaces — delegates to spawn_store functions
- D4: Hooks are post-dispatch only — fire after successful store write
- D5: keep update_spawn public — metadata only, no transition
- D9: No async — synchronous methods matching spawn_store
- D12: First-class LifecycleEvent over ad hoc callbacks
- D13: Metrics may be None on spawn.finished — fire for persisted terminal writes,
  including a terminal a runner writes over a derived one

Import note
-----------
Shared spawn record types live in ``meridian.lib.state.spawn.model`` and
launch-policy snapshot lives in ``meridian.lib.core.launch_policy_snapshot``.
Store access stays lazy so package-level compatibility re-exports do not
reintroduce cycles.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

import psutil
import structlog

from meridian.lib.core.domain import SpawnStatus, TerminalSpawnStatus
from meridian.lib.core.spawn_lifecycle import (
    SpawnReservation,
    is_terminal_spawn_status,
)
from meridian.lib.core.telemetry import (
    CORE_EVENTS,
    SpawnFailureCategory,
    allocate_spawn_sequence,
    next_spawn_sequence,
    notify_observers,
)
from meridian.lib.core.telemetry import (
    LifecycleEvent as TelemetryLifecycleEvent,
)
from meridian.lib.launch.signals import signal_coordinator
from meridian.lib.state.launch_lock import LaunchSlot
from meridian.lib.state.paths import LaunchPaths
from meridian.lib.state.session_identity import spawn_owner_chat_id
from meridian.lib.telemetry import LifecycleCorrelation, bind_lifecycle_correlation

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from meridian.lib.core.clock import Clock
    from meridian.lib.core.domain import TokenUsage
    from meridian.lib.hooks.dispatch import HookDispatcher
    from meridian.lib.state.spawn.model import (
        LaunchMode,
        SpawnOrigin,
        SpawnRecord,
    )
    from meridian.lib.state.spawn.repository import LockedMutationResult
    from meridian.plugin_api.types import SpawnKind

logger = structlog.get_logger(__name__)


def _pid_created_at_epoch(pid: int | None) -> float | None:
    if pid is None or pid <= 0:
        return None
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError, ValueError):
        return None


def _spawn_store() -> Any:
    """Return the spawn store module without importing it during lifecycle init."""

    from meridian.lib.state import spawn_store

    return spawn_store


def _locked_mutation_snapshot(outcome: LockedMutationResult) -> SpawnRecord | None:
    from meridian.lib.state.spawn.repository import Applied, Declined

    if isinstance(outcome, Applied):
        return outcome.after
    if isinstance(outcome, Declined):
        return outcome.snapshot
    return None


# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

EventType = Literal["spawn.created", "spawn.started", "spawn.finished"]
TerminalOrigin = Literal["runner", "launcher", "cancel", "reconciler", "launch_failure"]


type TerminalOutcomeCategory = Literal["succeeded"] | SpawnFailureCategory


# ---------------------------------------------------------------------------
# Event ID generation
# ---------------------------------------------------------------------------


def generate_event_id(spawn_id: str, n: int, event_type: str) -> UUID:
    """Generate a stable event ID for one launch of a spawn, using UUID v5.

    Launch 1 hashes to the ID the old always-zero ``sequence`` gave, so events of spawns that
    started before launches keep their identity; launch 2 would otherwise dedupe against 1.
    """

    namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # URL namespace
    name = f"meridian:spawn:{spawn_id}:{event_type}:{n - 1}"
    return uuid.uuid5(namespace, name)


# ---------------------------------------------------------------------------
# LifecycleEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleEvent:
    """Immutable event passed to lifecycle hooks.

    Provides stable identity for idempotent execution and full context
    for declarative filters.
    """

    # Identity
    event_id: UUID
    event_type: EventType
    timestamp: datetime

    # Spawn context (always present)
    spawn_id: str
    parent_id: str | None
    # Whose spawn: the owner's chat, not the child's own session chat.
    chat_id: str | None
    work_id: str | None

    # Execution context (always present)
    agent: str | None
    kind: SpawnKind
    model: str | None
    harness: str | None

    # Terminal-only fields (None for non-terminal events)
    status: TerminalSpawnStatus | None = None
    origin: TerminalOrigin | None = None
    error: str | None = None

    outcome_category: TerminalOutcomeCategory | None = None

    # Metrics (may be None even on terminal events — reducer may merge later)
    duration_secs: float | None = None
    total_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_is_estimate: bool = False


# ---------------------------------------------------------------------------
# LifecycleHook protocol
# ---------------------------------------------------------------------------


class LifecycleHook(Protocol):
    """Protocol for lifecycle event observers."""

    def on_event(self, event: LifecycleEvent) -> None:
        """Called after successful lifecycle state write.

        Called synchronously after the authoritative store write succeeds.
        Implementations should be fast — defer heavy work to background.

        Exceptions are logged but do not block lifecycle transitions.
        """
        ...


# ---------------------------------------------------------------------------
# SpawnLifecycleService
# ---------------------------------------------------------------------------


class SpawnLifecycleService:
    """Authoritative lifecycle API that wraps spawn_store with hook dispatch.

    This class is the intended caller-facing transition seam. It delegates
    persistence to ``spawn_store`` and only adds consistent post-write
    ``LifecycleEvent`` dispatch semantics.
    """

    def __init__(
        self,
        runtime_root: Path,
        *,
        hooks: list[LifecycleHook] | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._hooks = hooks or []
        self._record: SpawnRecord | None = None

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    def start(
        self,
        reservation: SpawnReservation,
        *,
        dispatch_events: bool = True,
        launch_lock: LaunchSlot,
    ) -> LaunchPaths:
        """Persist a spawn row from ``reservation``; return the launch it published."""
        return self._start_from_reservation(
            reservation, dispatch_events=dispatch_events, launch_lock=launch_lock
        )

    def reserve(self, reservation: SpawnReservation, *, launch_lock: LaunchSlot) -> LaunchPaths:
        """Persist a spawn row without dispatching lifecycle events."""
        return self.start(reservation, dispatch_events=False, launch_lock=launch_lock)

    def announce(self, spawn_id: str) -> None:
        """Dispatch spawn.created (a first launch only) and spawn.started for a launch's row."""
        with bind_lifecycle_correlation(self._correlation(operation="announce", spawn_id=spawn_id)):
            record = _spawn_store().get_spawn(self._runtime_root, spawn_id)
            if record is None:
                raise ValueError(f"spawn row not found: {spawn_id}")
            self._record = record
            self._dispatch_spawn_started_events(status=record.status)

    def _start_from_reservation(
        self,
        reservation: SpawnReservation,
        *,
        dispatch_events: bool,
        launch_lock: LaunchSlot,
    ) -> LaunchPaths:
        with bind_lifecycle_correlation(
            self._correlation(operation="start", spawn_id=reservation.spawn_id)
        ):
            session_metadata = reservation.session_metadata
            # Authoritative transition write still happens in _spawn_store().
            launch = _spawn_store().start_spawn(
                self._runtime_root,
                chat_id=reservation.chat_id,
                owner_chat_id=reservation.owner_chat_id,
                parent_id=reservation.parent_id,
                model=session_metadata.model,
                agent=session_metadata.agent,
                agent_path=session_metadata.agent_path or None,
                skills=session_metadata.skills,
                skill_paths=session_metadata.skill_paths,
                harness=session_metadata.harness,
                kind=reservation.kind,
                prompt=reservation.prompt,
                metadata=reservation.metadata,
                desc=reservation.desc,
                work_id=reservation.work_id,
                goal=reservation.goal,
                spawn_id=reservation.spawn_id,
                harness_session_id=reservation.harness_session_id,
                control_root=reservation.control_root,
                task_cwd=reservation.task_cwd,
                execution_cwd=reservation.execution_cwd,
                launch_mode=reservation.launch_mode,
                worker_pid=reservation.worker_pid,
                runner_pid=reservation.runner_pid,
                launch_policy_snapshot=reservation.launch_policy_snapshot,
                status=reservation.status,
                started_at=reservation.started_at,
                clock=reservation.clock,
                launch_lock=launch_lock,
            )
            allocate_spawn_sequence(str(launch.spawn_id))
            record = _spawn_store().get_spawn(self._runtime_root, launch.spawn_id)
            self._record = record
            if dispatch_events:
                self._dispatch_spawn_started_events(status=reservation.status)
            return launch

    def reopen(
        self,
        reservation: SpawnReservation,
        *,
        launch_lock: LaunchSlot,
        dispatch_events: bool = True,
    ) -> SpawnRecord:
        """Start the next launch of the spawn ``launch_lock`` holds; return its new row.

        ``reservation`` carries what the launch is told, as for ``start``: its model and
        paths replace the last launch's. The spawn's history, agent, skills and policy stay;
        ``desc`` and ``work_id`` replace the spawn's own only when set. Dispatches
        ``spawn.started`` (never ``spawn.created``).
        """
        store = _spawn_store()
        handle = launch_lock.handle
        with bind_lifecycle_correlation(
            self._correlation(operation="reopen", spawn_id=str(handle.spawn_id))
        ):
            row = store.reopen_spawn(
                handle,
                self._runtime_root,
                store.LaunchInput(
                    chat_id=reservation.chat_id,
                    prompt=reservation.prompt,
                    kind=reservation.kind,
                    model=reservation.session_metadata.model,
                    control_root=reservation.control_root,
                    task_cwd=reservation.task_cwd,
                    execution_cwd=reservation.execution_cwd,
                    owner_chat_id=reservation.owner_chat_id,
                    parent_id=reservation.parent_id,
                    goal=reservation.goal,
                    harness_session_id=reservation.harness_session_id,
                    launch_mode=reservation.launch_mode,
                    worker_pid=reservation.worker_pid,
                    runner_pid=reservation.runner_pid,
                    runner_created_at_epoch=None,
                ),
                desc=reservation.desc,
                work_id=reservation.work_id,
                clock=reservation.clock,
            )
            self._record = row
            if dispatch_events:
                self._dispatch_spawn_started_events(status=row.status)
            return row

    def _dispatch_spawn_started_events(self, *, status: SpawnStatus) -> None:
        assert self._record is not None
        if self._record.launch_number == _spawn_store().FIRST_LAUNCH:
            event = self._build_event("spawn.created", self._record)
            self._dispatch(event)
            self._emit_telemetry_event("spawn.created", self._record)
        if status == "running":
            event = self._build_event("spawn.started", self._record)
            self._dispatch(event)
            self._emit_telemetry_event("spawn.started", self._record)

    def record_launch_facts(
        self,
        spawn_id: str,
        *,
        launch_mode: LaunchMode | None = None,
        worker_pid: int | None = None,
        runner_pid: int | None = None,
        runner_created_at_epoch: float | None = None,
    ) -> None:
        """Record launch facts (modes and PIDs). Rows are created ``running``; no event."""
        with bind_lifecycle_correlation(
            self._correlation(operation="record_launch_facts", spawn_id=spawn_id)
        ):
            resolved_runner_created_at_epoch = runner_created_at_epoch
            if runner_pid is not None and resolved_runner_created_at_epoch is None:
                resolved_runner_created_at_epoch = _pid_created_at_epoch(runner_pid)
            outcome = _spawn_store().record_spawn_launch_facts(
                self._runtime_root,
                spawn_id,
                launch_mode=launch_mode,
                worker_pid=worker_pid,
                runner_pid=runner_pid,
                runner_created_at_epoch=resolved_runner_created_at_epoch,
            )
            record = _locked_mutation_snapshot(outcome)
            if self._owns_record(spawn_id) and record is not None:
                self._record = record

    def record_exited(self, spawn_id: str, *, exit_code: int) -> None:
        """Emit ``spawn.process_exited``. The exit code is telemetry only; the row keeps no copy."""
        with bind_lifecycle_correlation(
            self._correlation(operation="record_exited", spawn_id=spawn_id)
        ):
            self._emit_telemetry_event(
                "spawn.process_exited",
                _spawn_store().get_spawn(self._runtime_root, spawn_id),
                payload={"exit_code": exit_code},
            )

    def finalize(
        self,
        spawn_id: str,
        status: TerminalSpawnStatus,
        exit_code: int,
        *,
        origin: SpawnOrigin,
        duration_secs: float | None = None,
        usage: TokenUsage | None = None,
        finished_at: str | None = None,
        error: str | None = None,
        managed_scopes_pending: bool = False,
        expected_n: int | None = None,
        clock: Clock | None = None,
    ) -> LockedMutationResult:
        """Finalize a spawn and dispatch spawn.finished for persisted terminal writes."""
        requested_category = self._terminal_outcome_category(
            status=status,
            origin=origin,
            error=error,
        )
        with bind_lifecycle_correlation(
            self._correlation(
                operation="finalize",
                spawn_id=spawn_id,
                outcome_category=requested_category,
                terminal_status=status,
                terminal_origin=origin,
            )
        ):
            # A SIGTERM or SIGHUP (a closing terminal) mid-write would leave the launch reading as
            # crashed where it had finished, and one right after would kill the runner before the
            # hooks ran. The mask defers it past the hooks and re-delivers it on exit, so a cancel
            # racing the finish still gets ``spawn.finished``; its SIGKILL deadline bounds the wait.
            with signal_coordinator().mask_termination():
                outcome = _spawn_store().finalize_spawn(
                    self._runtime_root,
                    spawn_id,
                    status,
                    exit_code,
                    origin=origin,
                    duration_secs=duration_secs,
                    usage=usage,
                    finished_at=finished_at,
                    error=error,
                    managed_scopes_pending=managed_scopes_pending,
                    expected_n=expected_n,
                    clock=clock,
                )
                from meridian.lib.state.spawn.repository import Applied

                if isinstance(outcome, Applied):
                    snapshot = outcome.after
                    if self._owns_record(spawn_id):
                        self._record = snapshot
                    event = self._build_event("spawn.finished", snapshot)
                    self._dispatch(event)
                    self._emit_telemetry_event_for_record(f"spawn.{snapshot.status}", snapshot)
            return outcome

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _correlation(
        self,
        *,
        operation: str,
        record: SpawnRecord | None = None,
        spawn_id: str | None = None,
        event_type: str | None = None,
        outcome_category: TerminalOutcomeCategory | None = None,
        terminal_status: str | None = None,
        terminal_origin: str | None = None,
    ) -> LifecycleCorrelation:
        resolved_spawn_id = spawn_id or (record.id if record is not None else None)
        resolved_status = terminal_status
        if (
            resolved_status is None
            and record is not None
            and is_terminal_spawn_status(record.status)
        ):
            resolved_status = record.status
        resolved_origin = terminal_origin
        if resolved_origin is None and record is not None:
            resolved_origin = record.terminal.origin if record.terminal is not None else None
        return LifecycleCorrelation(
            spawn_id=resolved_spawn_id,
            parent_id=record.parent_id if record is not None else None,
            chat_id=spawn_owner_chat_id(record) if record is not None else None,
            work_id=record.work_id if record is not None else None,
            agent=record.agent if record is not None else None,
            model=record.model if record is not None else None,
            harness=record.harness if record is not None else None,
            lifecycle_operation=operation,
            lifecycle_event=event_type,
            terminal_status=resolved_status,
            terminal_origin=resolved_origin,
            failure_category=(str(outcome_category) if outcome_category is not None else None),
        )

    def _terminal_outcome_category(
        self,
        *,
        status: str,
        origin: str | None,
        error: str | None = None,
    ) -> TerminalOutcomeCategory:
        return _terminal_outcome_category(status=status, origin=origin, error=error)

    def bootstrap_from_disk(self, spawn_id: str) -> SpawnRecord | None:
        """Load owner state once for workers that did not call start()."""

        self._record = _spawn_store().get_spawn(
            self._runtime_root,
            spawn_id,
        )
        return self._record

    def _owns_record(self, spawn_id: str) -> bool:
        return self._record is not None and self._record.id == str(spawn_id)

    def _dispatch(self, event: LifecycleEvent) -> None:
        correlation = LifecycleCorrelation(
            spawn_id=event.spawn_id,
            parent_id=event.parent_id,
            chat_id=event.chat_id,
            work_id=event.work_id,
            agent=event.agent,
            model=event.model,
            harness=event.harness,
            lifecycle_operation="dispatch",
            lifecycle_event=event.event_type,
            terminal_status=event.status,
            terminal_origin=event.origin,
            failure_category=(
                str(event.outcome_category) if event.outcome_category is not None else None
            ),
        )
        with bind_lifecycle_correlation(correlation):
            for hook in self._hooks:
                try:
                    hook.on_event(event)
                except Exception:
                    logger.exception(
                        "Lifecycle hook raised exception; transition continues",
                        event_id=str(event.event_id),
                        event_type=event.event_type,
                        spawn_id=event.spawn_id,
                    )

    def _emit_telemetry_event(
        self,
        event_name: str,
        record: SpawnRecord | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if event_name not in CORE_EVENTS and event_name != "spawn.updated":
            return
        if record is None:
            return
        self._emit_telemetry_event_for_record(event_name, record, payload=payload)

    def _emit_telemetry_event_for_record(
        self,
        event_name: str,
        record: SpawnRecord,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if event_name not in CORE_EVENTS and event_name != "spawn.updated":
            return
        if payload is None and event_name in {
            "spawn.succeeded",
            "spawn.failed",
            "spawn.cancelled",
            "spawn.timed_out",
        }:
            payload = _terminal_telemetry_payload(record)
        _emit_lifecycle_event(event_name, record, payload=payload)

    def _build_event(
        self,
        event_type: EventType,
        record: SpawnRecord,
    ) -> LifecycleEvent:
        # Terminal-only fields
        status: TerminalSpawnStatus | None = None
        origin: TerminalOrigin | None = None
        error: str | None = None
        duration_secs: float | None = None
        total_cost_usd: float | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        cache_read_input_tokens: int | None = None
        cache_creation_input_tokens: int | None = None
        reasoning_tokens: int | None = None
        cost_is_estimate = False
        outcome_category: TerminalOutcomeCategory | None = None

        if event_type == "spawn.finished":
            rec_status = record.status
            if is_terminal_spawn_status(rec_status):
                status = rec_status
            terminal = record.terminal
            origin = terminal.origin if terminal is not None else None
            error = terminal.error if terminal is not None else None
            duration_secs = terminal.duration_secs if terminal is not None else None
            total_cost_usd = terminal.total_cost_usd if terminal is not None else None
            input_tokens = terminal.input_tokens if terminal is not None else None
            output_tokens = terminal.output_tokens if terminal is not None else None
            cache_read_input_tokens = (
                terminal.cache_read_input_tokens if terminal is not None else None
            )
            cache_creation_input_tokens = (
                terminal.cache_creation_input_tokens if terminal is not None else None
            )
            reasoning_tokens = terminal.reasoning_tokens if terminal is not None else None
            cost_is_estimate = terminal.cost_is_estimate if terminal is not None else False
            outcome_category = self._terminal_outcome_category(
                status=record.status,
                origin=terminal.origin if terminal is not None else None,
                error=terminal.error if terminal is not None else None,
            )

        return LifecycleEvent(
            event_id=generate_event_id(record.id, record.launch_number, event_type),
            event_type=event_type,
            timestamp=datetime.now(tz=UTC),
            spawn_id=record.id,
            parent_id=record.parent_id,
            chat_id=spawn_owner_chat_id(record),
            work_id=record.work_id,
            agent=record.agent,
            kind=record.kind,
            model=record.model,
            harness=record.harness,
            status=status,
            origin=origin,
            error=error,
            outcome_category=outcome_category,
            duration_secs=duration_secs,
            total_cost_usd=total_cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_is_estimate=cost_is_estimate,
        )


def _emit_lifecycle_event(
    event_name: str,
    spawn: SpawnRecord,
    *,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a telemetry lifecycle event through the observer registry."""
    event = TelemetryLifecycleEvent(
        event=event_name,
        spawn_id=str(spawn.id),
        harness_id=spawn.harness or "",
        model=spawn.model or "",
        agent=spawn.agent,
        ts=datetime.now(tz=UTC),
        seq=next_spawn_sequence(spawn.id),
        payload=payload or {},
    )
    notify_observers(event)


def _terminal_failure_category(
    *,
    status: str,
    origin: str | None,
    error: str | None = None,
) -> SpawnFailureCategory | None:
    _ = error
    if status == "succeeded":
        return None
    if status == "cancelled" or origin == "cancel":
        return SpawnFailureCategory.CANCELLATION
    if origin == "launch_failure":
        return SpawnFailureCategory.LAUNCH_FAILURE
    if origin == "reconciler":
        return SpawnFailureCategory.RECONCILER_ORPHAN
    if status != "failed":
        return SpawnFailureCategory.UNKNOWN_FAILURE
    if origin == "runner":
        return SpawnFailureCategory.HARNESS_FAILURE
    return SpawnFailureCategory.UNKNOWN_FAILURE


def _terminal_outcome_category(
    *,
    status: str,
    origin: str | None,
    error: str | None,
) -> TerminalOutcomeCategory:
    failure_category = _terminal_failure_category(status=status, origin=origin, error=error)
    if failure_category is not None:
        return failure_category
    return "succeeded"


def _terminal_telemetry_payload(spawn: SpawnRecord) -> dict[str, Any]:
    """Build sparse terminal lifecycle payload for observer projections."""
    terminal = spawn.terminal
    payload: dict[str, Any] = {
        "status": spawn.status,
    }
    failure_category = _terminal_failure_category(
        status=spawn.status,
        origin=terminal.origin if terminal is not None else None,
        error=terminal.error if terminal is not None else None,
    )
    if failure_category is not None:
        payload["category"] = str(failure_category)
    if terminal is None:
        return payload
    payload["exit_code"] = terminal.exit_code
    if terminal.duration_secs is not None:
        payload["duration_secs"] = terminal.duration_secs
    if terminal.total_cost_usd is not None:
        payload["total_cost_usd"] = terminal.total_cost_usd
    if terminal.input_tokens is not None:
        payload["input_tokens"] = terminal.input_tokens
    if terminal.output_tokens is not None:
        payload["output_tokens"] = terminal.output_tokens
    if terminal.cache_read_input_tokens is not None:
        payload["cache_read_input_tokens"] = terminal.cache_read_input_tokens
    if terminal.cache_creation_input_tokens is not None:
        payload["cache_creation_input_tokens"] = terminal.cache_creation_input_tokens
    if terminal.reasoning_tokens is not None:
        payload["reasoning_tokens"] = terminal.reasoning_tokens
    if terminal.cost_is_estimate:
        payload["cost_is_estimate"] = True
    if terminal.error:
        payload["reason"] = terminal.error
    return payload


# ---------------------------------------------------------------------------
# Lifecycle service factory seam
# ---------------------------------------------------------------------------


def _hooks_dispatch_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether hook dispatch should run globally."""

    scope = os.environ if env is None else env
    value = scope.get("MERIDIAN_HOOKS_ENABLED")
    if value is None:
        return True
    return value.strip().lower() != "false"


def get_hook_dispatcher(project_root: Path, runtime_root: Path) -> HookDispatcher | None:
    """Build a hook dispatcher when hook dispatch is enabled."""

    if not _hooks_dispatch_enabled():
        return None

    try:
        # Imported lazily to avoid lifecycle <-> hooks circular import at module import time.
        from meridian.lib.hooks.config import load_hooks_config_report
        from meridian.lib.hooks.dispatch import HookDispatcher
        from meridian.lib.hooks.registry import HookRegistry

        loaded_hooks = load_hooks_config_report(project_root)
        if loaded_hooks.errors:
            logger.warning(
                "Dropped invalid hook rows; continuing with valid lifecycle hooks.",
                error="; ".join(loaded_hooks.errors),
            )
        return HookDispatcher(
            project_root,
            runtime_root,
            registry=HookRegistry(project_root, hooks_config=loaded_hooks.config),
        )
    except ValueError as exc:
        logger.warning(
            "Invalid hook config; continuing without lifecycle hook dispatch.",
            error=str(exc),
        )
        return None
    except Exception:
        logger.exception(
            "Failed to build hook dispatcher; continuing without lifecycle hook dispatch."
        )
        return None


def create_lifecycle_service(
    project_root: Path,
    runtime_root: Path,
) -> SpawnLifecycleService:
    """Create a spawn lifecycle service with centralized hook wiring."""

    dispatcher = get_hook_dispatcher(project_root, runtime_root)
    hooks: list[LifecycleHook] | None = [dispatcher] if dispatcher is not None else None
    return SpawnLifecycleService(
        runtime_root,
        hooks=hooks,
    )
