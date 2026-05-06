"""Authoritative lifecycle-transition seam for spawn state.

This module is the single service boundary for lifecycle transitions
(``start``, ``mark_running``, ``record_exited``, ``mark_finalizing``,
``finalize``, ``cancel``) and post-write lifecycle hook dispatch.
Persistence remains delegated to :mod:`meridian.lib.state.spawn_store`.

Design decisions in effect:
- D1: Service wraps store, not replaces — delegates to spawn_store functions
- D4: Hooks are post-dispatch only — fire after successful store write
- D5: keep update_spawn public — metadata only, no transition
- D9: No async — synchronous methods matching spawn_store
- D12: First-class LifecycleEvent over ad hoc callbacks
- D13: Metrics may be None on spawn.finalized — fire for persisted terminal writes,
  including authoritative replacement of reconciler terminal state

Import note
-----------
``spawn_store`` is imported at the BOTTOM of this module to avoid a circular
import.  ``meridian.lib.state.__init__`` re-exports types from this module;
when Python resolves that package import it would re-enter this file.  By
placing the ``spawn_store`` import after all public symbols are defined, the
partially-initialised module already exposes ``LifecycleEvent``,
``LifecycleHook``, and ``SpawnLifecycleService`` when ``__init__.py`` asks for
them.  Method bodies reference ``spawn_store`` by name at call time, not at
definition time, so the delayed import is transparent to callers.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

import structlog

from meridian.lib.core.telemetry import (
    CORE_EVENTS,
    SpawnFailure,
    allocate_spawn_sequence,
    next_spawn_sequence,
    notify_observers,
)
from meridian.lib.core.telemetry import (
    LifecycleEvent as TelemetryLifecycleEvent,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from meridian.lib.core.clock import Clock
    from meridian.lib.core.domain import SpawnStatus
    from meridian.lib.hooks.dispatch import HookDispatcher
    from meridian.lib.state.spawn.repository import SpawnRepository
    from meridian.lib.state.spawn_store import FinalizeOutcome, LaunchMode, SpawnOrigin, SpawnRecord

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

EventType = Literal["spawn.created", "spawn.running", "spawn.finalized"]
TerminalStatus = Literal["succeeded", "failed", "cancelled"]
TerminalOrigin = Literal["runner", "launcher", "cancel", "reconciler", "launch_failure"]

_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

# ---------------------------------------------------------------------------
# Event ID generation
# ---------------------------------------------------------------------------


def _event_scope(event_type: str) -> str:
    """Return event namespace scope used in deterministic event IDs."""

    # Keep spawn event IDs byte-for-byte stable with legacy generation.
    if event_type.startswith("spawn."):
        return "spawn"
    return "event"


def generate_lifecycle_event_id(subject_id: str, event_type: str, sequence: int) -> UUID:
    """Generate stable event ID using UUID v5.

    Stability across retries: same subject_id + event_type + sequence
    always produces the same UUID.
    """
    namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # URL namespace
    name = f"meridian:{_event_scope(event_type)}:{subject_id}:{event_type}:{sequence}"
    return uuid.uuid5(namespace, name)


def generate_event_id(spawn_id: str, event_type: str, sequence: int) -> UUID:
    """Backward-compatible wrapper for spawn lifecycle callers."""

    return generate_lifecycle_event_id(spawn_id, event_type, sequence)


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
    chat_id: str | None
    work_id: str | None

    # Execution context (always present)
    agent: str | None
    model: str | None
    harness: str | None

    # Terminal-only fields (None for non-terminal events)
    status: TerminalStatus | None = None
    origin: TerminalOrigin | None = None

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
        repository: SpawnRepository | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._hooks = hooks or []
        self._repository = repository

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        chat_id: str,
        parent_id: str | None = None,
        model: str,
        agent: str,
        agent_path: str | None = None,
        skills: tuple[str, ...] = (),
        skill_paths: tuple[str, ...] = (),
        harness: str,
        kind: str = "child",
        prompt: str,
        desc: str | None = None,
        work_id: str | None = None,
        spawn_id: str | None = None,
        harness_session_id: str | None = None,
        execution_cwd: str | None = None,
        launch_mode: LaunchMode | None = None,
        worker_pid: int | None = None,
        runner_pid: int | None = None,
        status: SpawnStatus = "running",
        started_at: str | None = None,
        clock: Clock | None = None,
    ) -> str:
        """Start a new spawn and dispatch spawn.created."""
        # Authoritative transition write still happens in spawn_store.
        result_id = spawn_store.start_spawn(
            self._runtime_root,
            chat_id=chat_id,
            parent_id=parent_id,
            model=model,
            agent=agent,
            agent_path=agent_path,
            skills=skills,
            skill_paths=skill_paths,
            harness=harness,
            kind=kind,
            prompt=prompt,
            desc=desc,
            work_id=work_id,
            spawn_id=spawn_id,
            harness_session_id=harness_session_id,
            execution_cwd=execution_cwd,
            launch_mode=launch_mode,
            worker_pid=worker_pid,
            runner_pid=runner_pid,
            status=status,
            started_at=started_at,
            clock=clock,
            repository=self._repository,
        )
        allocate_spawn_sequence(str(result_id))
        event = self._build_event("spawn.created", str(result_id))
        self._dispatch(event)
        self._emit_telemetry_event("spawn.queued", str(result_id))
        if status == "running":
            self._emit_telemetry_event("spawn.running", str(result_id))
        return str(result_id)

    def mark_running(
        self,
        spawn_id: str,
        *,
        launch_mode: LaunchMode | None = None,
        worker_pid: int | None = None,
        runner_pid: int | None = None,
    ) -> None:
        """Mark a spawn as running and dispatch spawn.running."""
        previous = spawn_store.get_spawn(
            self._runtime_root, spawn_id, repository=self._repository
        )
        # Authoritative transition write still happens in spawn_store.
        changed = spawn_store.mark_spawn_running(
            self._runtime_root,
            spawn_id,
            launch_mode=launch_mode,
            worker_pid=worker_pid,
            runner_pid=runner_pid,
            repository=self._repository,
        )
        if changed:
            event = self._build_event("spawn.running", spawn_id)
            self._dispatch(event)
        if changed and (previous is None or previous.status != "running"):
            self._emit_telemetry_event("spawn.running", spawn_id)

    def record_exited(
        self,
        spawn_id: str,
        *,
        exit_code: int,
        exited_at: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Record process exit and emit spawn.process_exited telemetry."""
        # Authoritative transition write still happens in spawn_store.
        spawn_store.record_spawn_exited(
            self._runtime_root,
            spawn_id,
            exit_code=exit_code,
            exited_at=exited_at,
            clock=clock,
            repository=self._repository,
        )
        self._emit_telemetry_event(
            "spawn.process_exited",
            spawn_id,
            payload={"exit_code": exit_code},
        )

    def finalize(
        self,
        spawn_id: str,
        status: SpawnStatus,
        exit_code: int,
        *,
        origin: SpawnOrigin,
        duration_secs: float | None = None,
        total_cost_usd: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cost_is_estimate: bool = False,
        finished_at: str | None = None,
        error: str | None = None,
        clock: Clock | None = None,
    ) -> FinalizeOutcome:
        """Finalize a spawn and dispatch spawn.finalized for persisted terminal writes."""
        # Authoritative transition write still happens in spawn_store.
        outcome = spawn_store.finalize_spawn(
            self._runtime_root,
            spawn_id,
            status,
            exit_code,
            origin=origin,
            duration_secs=duration_secs,
            total_cost_usd=total_cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_is_estimate=cost_is_estimate,
            finished_at=finished_at,
            error=error,
            clock=clock,
            repository=self._repository,
        )
        if outcome.wrote and outcome.snapshot is not None:
            if outcome.snapshot.status == "failed":
                _write_failure_sentinel(
                    self._runtime_root,
                    spawn_id,
                    SpawnFailure(
                        spawn_id=spawn_id,
                        ts=datetime.now(tz=UTC),
                        exit_code=outcome.snapshot.exit_code,
                        reason=outcome.snapshot.error
                        or outcome.snapshot.terminal_origin
                        or origin,
                        metadata={"origin": outcome.snapshot.terminal_origin or origin},
                    ),
                )
            event = self._build_event_from_record("spawn.finalized", outcome.snapshot)
            self._dispatch(event)
            self._emit_telemetry_event_for_record(
                f"spawn.{outcome.snapshot.status}", outcome.snapshot
            )
        return outcome

    def mark_finalizing(self, spawn_id: str) -> bool:
        """CAS transition running -> finalizing.  No lifecycle event dispatched."""
        # Authoritative transition write still happens in spawn_store.
        transitioned = spawn_store.mark_finalizing(
            self._runtime_root,
            spawn_id,
            repository=self._repository,
        )
        if transitioned:
            self._emit_telemetry_event("spawn.finalizing", spawn_id)
        return transitioned

    def cancel(
        self,
        spawn_id: str,
        exit_code: int = 130,
        *,
        duration_secs: float | None = None,
        total_cost_usd: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        cost_is_estimate: bool = False,
        finished_at: str | None = None,
        error: str | None = None,
        clock: Clock | None = None,
    ) -> bool:
        """Cancel a spawn — convenience for finalize(status='cancelled', origin='cancel')."""
        outcome = self.finalize(
            spawn_id,
            "cancelled",
            exit_code,
            origin="cancel",
            duration_secs=duration_secs,
            total_cost_usd=total_cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_is_estimate=cost_is_estimate,
            finished_at=finished_at,
            error=error,
            clock=clock,
        )
        return outcome.transitioned

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dispatch(self, event: LifecycleEvent) -> None:
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
        spawn_id: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if event_name not in CORE_EVENTS and event_name != "spawn.updated":
            return
        record = spawn_store.get_spawn(
            self._runtime_root, spawn_id, repository=self._repository
        )
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
        }:
            payload = _terminal_telemetry_payload(record)
        _emit_lifecycle_event(event_name, record, payload=payload)

    def _build_event(self, event_type: EventType, spawn_id: str) -> LifecycleEvent:
        # Read event payload through the same authoritative store boundary.
        record = spawn_store.get_spawn(
            self._runtime_root, spawn_id, repository=self._repository
        )

        # Terminal-only fields
        status: TerminalStatus | None = None
        origin: TerminalOrigin | None = None
        duration_secs: float | None = None
        total_cost_usd: float | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        cache_read_input_tokens: int | None = None
        cache_creation_input_tokens: int | None = None
        reasoning_tokens: int | None = None
        cost_is_estimate = False

        if event_type == "spawn.finalized" and record is not None:
            rec_status = record.status
            if rec_status in _TERMINAL_STATUS_VALUES:
                status = rec_status  # type: ignore[assignment]
            origin = record.terminal_origin  # type: ignore[assignment]
            duration_secs = record.duration_secs
            total_cost_usd = record.total_cost_usd
            input_tokens = record.input_tokens
            output_tokens = record.output_tokens
            cache_read_input_tokens = record.cache_read_input_tokens
            cache_creation_input_tokens = record.cache_creation_input_tokens
            reasoning_tokens = record.reasoning_tokens
            cost_is_estimate = record.cost_is_estimate

        return LifecycleEvent(
            event_id=generate_event_id(spawn_id, event_type, 0),
            event_type=event_type,
            timestamp=datetime.now(tz=UTC),
            spawn_id=spawn_id,
            parent_id=record.parent_id if record is not None else None,
            chat_id=record.chat_id if record is not None else None,
            work_id=record.work_id if record is not None else None,
            agent=record.agent if record is not None else None,
            model=record.model if record is not None else None,
            harness=record.harness if record is not None else None,
            status=status,
            origin=origin,
            duration_secs=duration_secs,
            total_cost_usd=total_cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_is_estimate=cost_is_estimate,
        )

    def _build_event_from_record(
        self,
        event_type: EventType,
        record: SpawnRecord,
    ) -> LifecycleEvent:
        status: TerminalStatus | None = None
        origin: TerminalOrigin | None = None
        duration_secs: float | None = None
        total_cost_usd: float | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        cache_read_input_tokens: int | None = None
        cache_creation_input_tokens: int | None = None
        reasoning_tokens: int | None = None
        cost_is_estimate = False

        if event_type == "spawn.finalized":
            rec_status = record.status
            if rec_status in _TERMINAL_STATUS_VALUES:
                status = rec_status  # type: ignore[assignment]
            origin = record.terminal_origin  # type: ignore[assignment]
            duration_secs = record.duration_secs
            total_cost_usd = record.total_cost_usd
            input_tokens = record.input_tokens
            output_tokens = record.output_tokens
            cache_read_input_tokens = record.cache_read_input_tokens
            cache_creation_input_tokens = record.cache_creation_input_tokens
            reasoning_tokens = record.reasoning_tokens
            cost_is_estimate = record.cost_is_estimate

        return LifecycleEvent(
            event_id=generate_event_id(record.id, event_type, 0),
            event_type=event_type,
            timestamp=datetime.now(tz=UTC),
            spawn_id=record.id,
            parent_id=record.parent_id,
            chat_id=record.chat_id,
            work_id=record.work_id,
            agent=record.agent,
            model=record.model,
            harness=record.harness,
            status=status,
            origin=origin,
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


def _terminal_telemetry_payload(spawn: SpawnRecord) -> dict[str, Any]:
    """Build sparse terminal lifecycle payload for observer projections."""
    payload: dict[str, Any] = {"status": spawn.status}
    if spawn.exit_code is not None:
        payload["exit_code"] = spawn.exit_code
    if spawn.duration_secs is not None:
        payload["duration_secs"] = spawn.duration_secs
    if spawn.error:
        payload["reason"] = spawn.error
    return payload


def _write_failure_sentinel(
    runtime_root: Path,
    spawn_id: str,
    failure: SpawnFailure,
) -> None:
    """Best-effort write of failure sentinel.

    Does not propagate exceptions; terminal state writes take priority.
    """
    try:
        from meridian.lib.state.paths import RuntimePaths

        sentinel_path = (
            RuntimePaths.from_root_dir(runtime_root).spawns_dir
            / spawn_id
            / "failure.json"
        )
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(failure)
        data["ts"] = failure.ts.isoformat()
        sentinel_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Failed to write failure sentinel for %s", spawn_id)


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
        from meridian.lib.hooks.dispatch import HookDispatcher
    except Exception:
        logger.exception(
            "Failed to import hook dispatcher; continuing without lifecycle hook dispatch."
        )
        return None

    return HookDispatcher(project_root, runtime_root)


def create_lifecycle_service(
    project_root: Path,
    runtime_root: Path,
    *,
    repository: SpawnRepository | None = None,
) -> SpawnLifecycleService:
    """Create a spawn lifecycle service with centralized hook wiring."""

    dispatcher = get_hook_dispatcher(project_root, runtime_root)
    hooks: list[LifecycleHook] | None = [dispatcher] if dispatcher is not None else None
    return SpawnLifecycleService(
        runtime_root,
        hooks=hooks,
        repository=repository,
    )


# ---------------------------------------------------------------------------
# Deferred import — must be at module bottom to break circular dependency with
# meridian.lib.state.__init__ which re-exports types from this module.
# ---------------------------------------------------------------------------

import meridian.lib.state.spawn_store as spawn_store  # noqa: E402
