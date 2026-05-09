"""Shared spawn application service for all surfaces."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_lifecycle import (
    ExecutionTerminalFacts,
    ExecutionTerminalOutcome,
    resolve_execution_terminal_outcome,
)
from meridian.lib.core.spawn_start import SpawnStartMetadata
from meridian.lib.core.telemetry import (
    LifecycleEvent,
    LifecycleObserver,
    LifecycleObserverTier,
    SpawnFailure,
    SpawnFailureCategory,
    next_spawn_sequence,
    notify_observers,
    register_observer,
)
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.launch.context import LaunchContext, build_launch_context
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.state import spawn_store
from meridian.lib.state.liveness import is_process_alive
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.model import LaunchMode, SpawnOrigin
from meridian.lib.streaming.signal_canceller import CancelOutcome as SignalCancelOutcome

if TYPE_CHECKING:
    from meridian.lib.harness.registry import HarnessRegistry
    from meridian.lib.observability.debug_tracer import DebugTracer
    from meridian.lib.state.primary_meta import PrimaryMetadata
    from meridian.lib.state.spawn.model import SpawnRecord
    from meridian.lib.streaming.spawn_manager import SpawnManager


_WAIT_POLL_INTERVAL_SECS = 0.1
_MANAGED_CANCEL_GRACE_SECS = 5.0
_MANAGED_CANCEL_FALLBACK_WAIT_SECS = 1.0


@dataclass(frozen=True)
class CancelOutcome:
    """Surface-neutral result of cancelling a spawn."""

    spawn_id: str
    status: SpawnStatus
    origin: SpawnOrigin
    exit_code: int
    already_terminal: bool = False
    finalizing: bool = False
    model: str | None = None
    harness: str | None = None




@dataclass(frozen=True)
class CompleteSpawnOutcome:
    """Surface-neutral result of a spawn finalization attempt."""

    wrote: bool
    transitioned: bool
    entered_finalizing: bool
    already_terminal: bool
    snapshot: SpawnRecord | None
    spawn_id: SpawnId

    @property
    def accepted(self) -> bool:
        """True when finalization was written (first OR replacement)."""
        return self.wrote


@dataclass(frozen=True)
class CompleteExecutionOutcome:
    """Lifecycle-owned terminal resolution plus persisted completion result."""

    resolved: ExecutionTerminalOutcome
    completion: CompleteSpawnOutcome


@dataclass(frozen=True)
class PreparedSpawn:
    """Result of successful spawn preparation.

    Contains everything a surface needs to start execution.
    Surfaces consume this — they never construct ConnectionConfig
    or call lifecycle_service.start() directly.

    SEAM-1: Row is only created after resolution succeeds.
    SEAM-2: resolved_model/agent/harness are never placeholders.
    SEAM-3: connection_config is projected from launch_context.
    """

    spawn_id: SpawnId
    launch_context: LaunchContext
    connection_config: ConnectionConfig
    resolved_model: str
    resolved_agent: str | None
    resolved_harness: str
    work_id: str | None


@dataclass(frozen=True)
class PrepareSpawnRequest:
    """Typed request for surface-neutral spawn preparation."""

    request: SpawnRequest
    runtime: LaunchRuntime
    harness_registry: HarnessRegistry
    chat_id: str | None = None
    parent_id: str | None = None
    kind: str = "child"
    desc: str | None = None
    work_id: str | None = None
    launch_mode: LaunchMode | None = None
    runner_pid: int | None = None
    initial_status: SpawnStatus = "queued"
    debug_tracer: DebugTracer | None = None


class KeyedLockRegistry:
    """In-process keyed lock registry for spawn serialization.

    This registry is intentionally scoped to one ``SpawnApplicationService``
    instance. Multiple service instances in the same process do not share these
    locks; cross-instance and cross-process safety comes from the spawn store's
    file-level locking. A process-wide shared service/registry may reduce local
    races later, but Phase 0 treats this as a best-effort per-instance guard.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def acquire(self, key: str) -> asyncio.Lock:
        """Get or create a lock for the given key."""
        async with self._registry_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def release(self, key: str) -> None:
        """Keep lock mappings for the process lifetime.

        Removing a key can race with waiters that already hold the old lock
        instance, allowing a later caller to create a second lock for the same
        key and break per-key serialization. The registry is in-process and
        spawn keys are bounded enough that retaining locks is the safer tradeoff.
        """
        _ = key


class SpawnApplicationService:
    """Shared spawn application logic for all surfaces.

    Owns: validation, creation prep, cancel orchestration, finalize orchestration,
          archive, query, lifecycle broadcasting.
    Does NOT own: execution backend selection, surface-specific formatting.
    """

    def __init__(
        self,
        runtime_root: Path,
        lifecycle_service: SpawnLifecycleService,
        *,
        spawn_manager: SpawnManager | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._lifecycle = lifecycle_service
        self._spawn_manager = spawn_manager
        self._locks = KeyedLockRegistry()

    @property
    def runtime_root(self) -> Path:
        """Return the runtime root directory."""
        return self._runtime_root

    @property
    def lifecycle(self) -> SpawnLifecycleService:
        """Return the lifecycle authority backing this application service."""
        return self._lifecycle

    def register_observer(
        self,
        observer: LifecycleObserver,
        tier: LifecycleObserverTier = LifecycleObserverTier.DIAGNOSTIC,
    ) -> None:
        """Register a lifecycle observer.

        Diagnostic observers are best-effort: exceptions are logged and
        swallowed. Policy observers are part of the control path: exceptions
        propagate from the global telemetry dispatcher.
        """
        register_observer(observer, tier)

    # ---- Query Helpers ----

    def get_spawn(self, spawn_id: SpawnId | str) -> SpawnRecord | None:
        """Get spawn record by ID."""
        return spawn_store.get_spawn(self._runtime_root, spawn_id)

    def require_spawn(self, spawn_id: SpawnId | str) -> SpawnRecord:
        """Get spawn or raise ValueError."""
        record = self.get_spawn(spawn_id)
        if record is None:
            raise ValueError(f"Spawn '{spawn_id}' not found")
        return record

    def is_terminal(self, status: str) -> bool:
        """Check if status is terminal."""
        from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES

        return status in TERMINAL_SPAWN_STATUSES

    def require_not_terminal(self, record: SpawnRecord) -> None:
        """Raise if spawn is already terminal."""
        if self.is_terminal(record.status):
            raise ValueError(f"Spawn is already {record.status}")

    def require_not_finalizing(self, record: SpawnRecord) -> None:
        """Raise if spawn is currently finalizing."""
        if record.status == "finalizing":
            raise ValueError("Spawn is finalizing")

    def get_spawn_failure(self, spawn_id: SpawnId) -> SpawnFailure | None:
        """Read the failure sentinel for a spawn, if it exists."""
        record = self.get_spawn(spawn_id)
        if record is None or record.status != "failed":
            return None
        sentinel_path = (
            RuntimePaths.from_root_dir(self._runtime_root).spawns_dir
            / str(spawn_id)
            / "failure.json"
        )
        if not sentinel_path.exists():
            return None
        try:
            data = json.loads(sentinel_path.read_text(encoding="utf-8"))
            data["ts"] = datetime.fromisoformat(data["ts"])
            category = data.get("category")
            if isinstance(category, str):
                data["category"] = SpawnFailureCategory(category)
            return SpawnFailure(**data)
        except Exception:
            return None

    # ---- Spawn Preparation (SEAM-1, SEAM-2, SEAM-3) ----

    async def prepare(self, payload: PrepareSpawnRequest) -> PreparedSpawn:
        """Resolve launch context, create spawn row, and project ConnectionConfig.

        SEAM-1: No spawn row is created until build_launch_context() succeeds.
        SEAM-2: Row metadata always reflects resolved values (never "unknown").
        SEAM-3: ConnectionConfig is projected from LaunchContext.
        SEAM-ID.1: ID allocation happens atomically with row creation.

        Raises on resolution failure. No spawn row exists on failure.
        On success, row exists with resolved metadata.
        """
        # Generate a placeholder spawn_id for the first launch-context build.
        # Resolution can fail here; no spawn row or lifecycle event is emitted.
        temp_spawn_id = f"pending-{id(payload.request)}"

        # SEAM-1: Build launch context FIRST. This can fail.
        # If it fails, no spawn row exists.
        launch_ctx = await asyncio.to_thread(
            build_launch_context,
            spawn_id=temp_spawn_id,
            request=payload.request,
            runtime=payload.runtime,
            harness_registry=payload.harness_registry,
        )

        # Extract resolved metadata from launch context
        resolved_request = launch_ctx.resolved_request
        resolved_model = (resolved_request.model or "").strip()
        resolved_harness = (resolved_request.harness or "").strip()
        resolved_agent = (resolved_request.agent or "").strip() or None

        # Validate we have required fields
        if not resolved_harness:
            raise ValueError("Harness resolution failed - harness is required")

        # Resolve work_id
        effective_work_id = (payload.work_id or launch_ctx.work_id or "").strip() or None

        # Reserve the final ID and rebuild launch context before lifecycle.start
        # so MERIDIAN_SPAWN_ID and related env_overrides are final, while a
        # second-build failure still leaves no row and emits no ghost events.
        final_spawn_id = await asyncio.to_thread(
            spawn_store.reserve_spawn_id,
            self._runtime_root,
        )
        launch_ctx = await asyncio.to_thread(
            build_launch_context,
            spawn_id=str(final_spawn_id),
            request=payload.request,
            runtime=payload.runtime,
            harness_registry=payload.harness_registry,
        )
        resolved_request = launch_ctx.resolved_request
        resolved_model = (resolved_request.model or "").strip()
        resolved_harness = (resolved_request.harness or "").strip()
        resolved_agent = (resolved_request.agent or "").strip() or None
        if not resolved_harness:
            raise ValueError("Harness resolution failed - harness is required")
        effective_work_id = (payload.work_id or launch_ctx.work_id or "").strip() or None
        start_metadata = SpawnStartMetadata(
            desc=payload.desc,
            work_id=effective_work_id,
            goal=getattr(resolved_request, "goal", None),
        )

        # SEAM-ID.1: Persist the already-reserved ID via lifecycle service only
        # after launch context composition succeeds. Failed composition leaves no
        # spawn row and emits no spawn.created hook/telemetry.
        persisted_spawn_id = SpawnId(
            await asyncio.to_thread(
                self._lifecycle.start,
                chat_id=payload.chat_id or "",
                parent_id=payload.parent_id,
                model=resolved_model,
                agent=resolved_agent or "",
                agent_path=resolved_request.agent_metadata.get("session_agent_path"),
                skills=resolved_request.skills,
                skill_paths=resolved_request.skill_paths,
                harness=resolved_harness,
                kind=payload.kind,
                prompt=resolved_request.prompt,
                metadata=start_metadata,
                spawn_id=str(final_spawn_id),
                harness_session_id=resolved_request.session.requested_harness_session_id,
                execution_cwd=str(launch_ctx.child_cwd),
                launch_mode=payload.launch_mode,
                runner_pid=payload.runner_pid,
                status=payload.initial_status,
            )
        )
        if persisted_spawn_id != final_spawn_id:
            raise RuntimeError(
                f"Reserved spawn ID {final_spawn_id} but persisted {persisted_spawn_id}"
            )

        # SEAM-3: Project ConnectionConfig from LaunchContext
        harness_id = HarnessId(resolved_harness)
        connection_config = ConnectionConfig(
            spawn_id=final_spawn_id,
            harness_id=harness_id,
            prompt=launch_ctx.resolved_request.prompt,
            project_root=launch_ctx.child_cwd,
            env_overrides=dict(launch_ctx.env_overrides),
            system=launch_ctx.run_params.appended_system_prompt,
            debug_tracer=payload.debug_tracer,
        )

        return PreparedSpawn(
            spawn_id=final_spawn_id,
            launch_context=launch_ctx,
            connection_config=connection_config,
            resolved_model=resolved_model,
            resolved_agent=resolved_agent,
            resolved_harness=resolved_harness,
            work_id=effective_work_id,
        )

    async def prepare_spawn(
        self,
        *,
        request: SpawnRequest,
        runtime: LaunchRuntime,
        harness_registry: HarnessRegistry,
        chat_id: str | None = None,
        parent_id: str | None = None,
        kind: str = "child",
        desc: str | None = None,
        work_id: str | None = None,
        launch_mode: LaunchMode | None = None,
        runner_pid: int | None = None,
        initial_status: SpawnStatus = "queued",
        debug_tracer: DebugTracer | None = None,
    ) -> PreparedSpawn:
        """Compatibility wrapper over the typed ``prepare`` request API."""

        return await self.prepare(
            PrepareSpawnRequest(
                request=request,
                runtime=runtime,
                harness_registry=harness_registry,
                chat_id=chat_id,
                parent_id=parent_id,
                kind=kind,
                desc=desc,
                work_id=work_id,
                launch_mode=launch_mode,
                runner_pid=runner_pid,
                initial_status=initial_status,
                debug_tracer=debug_tracer,
            )
        )

    # ---- Spawn Operations ----

    async def cancel(self, spawn_id: SpawnId) -> CancelOutcome:
        """Cancel a spawn through the shared surface-neutral pipeline."""
        lock = await self._locks.acquire(str(spawn_id))
        async with lock:
            record = self.require_spawn(spawn_id)
            if self.is_terminal(record.status):
                self._cleanup_orphan_managed_primary(spawn_id, record)
                return _cancel_outcome_from_record(str(spawn_id), record, already_terminal=True)

            is_finalizing = record.status == "finalizing"
            from meridian.lib.state.primary_meta import read_primary_metadata

            primary_metadata = read_primary_metadata(self._runtime_root, str(spawn_id))

            if not is_finalizing:
                if primary_metadata is not None and primary_metadata.managed_backend:
                    return await self._cancel_managed_primary(spawn_id, record, primary_metadata)

                from meridian.lib.streaming.signal_canceller import SignalCanceller

                signal_outcome = await SignalCanceller(
                    runtime_root=self._runtime_root,
                    manager=self._spawn_manager,
                    complete_spawn=self._complete_spawn_unlocked,
                ).cancel(spawn_id)
                latest = self.get_spawn(spawn_id) or record
                return _cancel_outcome_from_signal(str(spawn_id), signal_outcome, latest)

        if is_finalizing:
            terminal = await self._wait_for_terminal(spawn_id, timeout=5.0)
            lock = await self._locks.acquire(str(spawn_id))
            async with lock:
                latest = terminal or self.get_spawn(spawn_id) or record
                return _cancel_outcome_from_record(
                    str(spawn_id),
                    latest,
                    already_terminal=terminal is not None,
                    finalizing=terminal is None,
                )
        raise RuntimeError("unreachable cancel state")

    def _cleanup_orphan_managed_primary(
        self,
        spawn_id: SpawnId,
        record: SpawnRecord,
    ) -> None:
        """Best-effort cleanup for terminal managed orphan-primary spawns."""
        if record.error != "orphan_primary":
            return

        from meridian.lib.state.managed_primary import terminate_managed_primary_processes
        from meridian.lib.state.primary_meta import read_primary_metadata

        metadata = read_primary_metadata(self._runtime_root, str(spawn_id))
        if metadata is not None:
            if not metadata.managed_backend:
                return
            terminate_managed_primary_processes(
                metadata,
                started_epoch=_started_at_epoch(record.started_at),
                include_launcher=False,
            )
            return

        if not _is_managed_primary_candidate(record):
            return
        from meridian.lib.core.process_cleanup import cancel_managed_primary
        from meridian.lib.state.process_scope_projection import read_scopes_from_disk

        scopes = read_scopes_from_disk(self._runtime_root, SpawnId(str(spawn_id)))
        if scopes:
            # Phase-3 scope records: use sequenced managed-primary teardown.
            cancel_managed_primary(
                self._runtime_root,
                record,
                grace_seconds=5.0,
            )
        else:
            # Legacy fallback: no scope records, use worker_pid termination.
            from meridian.lib.core.process_cleanup import terminate_spawn_scopes
            terminate_spawn_scopes(
                self._runtime_root,
                record,
                reason="cancel",
                grace_seconds=5.0,
            )

    async def _wait_for_terminal(
        self,
        spawn_id: SpawnId,
        *,
        timeout: float,
    ) -> SpawnRecord | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            current = self.get_spawn(spawn_id)
            if current is not None and self.is_terminal(current.status):
                return current
            now = time.monotonic()
            if now >= deadline:
                return None
            await asyncio.sleep(min(_WAIT_POLL_INTERVAL_SECS, deadline - now))

    async def _cancel_managed_primary(
        self,
        spawn_id: SpawnId,
        record: SpawnRecord,
        primary_metadata: PrimaryMetadata,
    ) -> CancelOutcome:
        from meridian.lib.state.managed_primary import terminate_managed_primary_processes

        if self.is_terminal(record.status):
            return _cancel_outcome_from_record(str(spawn_id), record, already_terminal=True)

        started_epoch = _started_at_epoch(record.started_at)
        launcher_pid = primary_metadata.launcher_pid
        launcher_alive = (
            launcher_pid is not None
            and is_process_alive(launcher_pid, created_after_epoch=started_epoch)
        )
        if launcher_alive:
            terminate_managed_primary_processes(
                primary_metadata,
                started_epoch=started_epoch,
                include_launcher=True,
                include_runtime_children=False,
            )
        else:
            terminate_managed_primary_processes(
                primary_metadata,
                started_epoch=started_epoch,
                include_launcher=False,
            )

        latest = await self._wait_for_terminal(
            spawn_id,
            timeout=_MANAGED_CANCEL_GRACE_SECS,
        )
        if latest is None and launcher_alive:
            terminate_managed_primary_processes(
                primary_metadata,
                started_epoch=started_epoch,
                include_launcher=False,
            )
            latest = await self._wait_for_terminal(
                spawn_id,
                timeout=_MANAGED_CANCEL_FALLBACK_WAIT_SECS,
            )

        if latest is None:
            latest = self.get_spawn(spawn_id) or record
            if latest.status == "finalizing":
                return _cancel_outcome_from_record(str(spawn_id), latest, finalizing=True)
            if self.is_terminal(latest.status):
                return _cancel_outcome_from_record(
                    str(spawn_id),
                    latest,
                    already_terminal=True,
                )

            if self._lifecycle.mark_finalizing(str(spawn_id)):
                latest = self.get_spawn(spawn_id) or latest
            else:
                latest = self.get_spawn(spawn_id) or latest
                if latest.status == "finalizing":
                    return _cancel_outcome_from_record(str(spawn_id), latest, finalizing=True)
                if self.is_terminal(latest.status):
                    return _cancel_outcome_from_record(
                        str(spawn_id),
                        latest,
                        already_terminal=True,
                    )
                self._lifecycle.finalize(
                    str(spawn_id),
                    "failed",
                    1,
                    origin="cancel",
                    error="cancel_timeout",
                )
                latest = self.get_spawn(spawn_id) or latest

        # Best-effort: release any Phase-3 scope records so the reaper does not
        # re-terminate processes that the metadata path already signalled.
        latest_for_cleanup = self.get_spawn(spawn_id) or latest
        from meridian.lib.core.process_cleanup import cancel_managed_primary
        await asyncio.to_thread(
            cancel_managed_primary,
            self._runtime_root,
            latest_for_cleanup,
            grace_seconds=0.0,
        )

        return _cancel_outcome_from_record(
            str(spawn_id),
            latest,
            finalizing=latest.status == "finalizing",
        )

    async def complete_spawn(
        self,
        spawn_id: SpawnId,
        status: str,
        exit_code: int,
        *,
        origin: str,
        duration_secs: float | None = None,
        **metrics: object,
    ) -> CompleteSpawnOutcome:
        """Finalize a spawn through the shared idempotent terminal seam.

        Returns a rich outcome distinguishing accepted writes, first
        transitions, pre-existing terminal rows, and post-write snapshots.
        """
        lock = await self._locks.acquire(str(spawn_id))
        async with lock:
            return await self._complete_spawn_unlocked(
                spawn_id,
                status,
                exit_code,
                origin=origin,
                duration_secs=duration_secs,
                **metrics,
            )

    async def complete_execution(
        self,
        spawn_id: SpawnId,
        facts: ExecutionTerminalFacts,
        *,
        origin: str,
        duration_secs: float | None = None,
        **metrics: object,
    ) -> CompleteExecutionOutcome:
        """Resolve execution facts, then finalize through the lifecycle authority."""

        resolved = resolve_execution_terminal_outcome(facts)
        completion = await self.complete_spawn(
            spawn_id,
            resolved.status,
            resolved.exit_code,
            origin=origin,
            duration_secs=duration_secs,
            error=resolved.error,
            **metrics,
        )
        return CompleteExecutionOutcome(
            resolved=resolved,
            completion=completion,
        )

    async def _complete_spawn_unlocked(
        self,
        spawn_id: SpawnId,
        status: str,
        exit_code: int,
        *,
        origin: str,
        duration_secs: float | None = None,
        **metrics: object,
    ) -> CompleteSpawnOutcome:
        record = self.get_spawn(spawn_id)
        if record is None:
            return CompleteSpawnOutcome(
                wrote=False,
                transitioned=False,
                entered_finalizing=False,
                already_terminal=False,
                snapshot=None,
                spawn_id=spawn_id,
            )
        was_terminal = self.is_terminal(record.status)

        entered_finalizing = False
        if record.status == "running":
            entered_finalizing = self._lifecycle.mark_finalizing(str(spawn_id))

        outcome = self._lifecycle.finalize(
            str(spawn_id),
            cast("SpawnStatus", status),
            exit_code,
            origin=cast("SpawnOrigin", origin),
            duration_secs=duration_secs,
            **cast("dict[str, Any]", metrics),
        )
        if outcome.wrote:
            from meridian.lib.state.process_scope_projection import (
                mark_scope_released,
                read_scopes_from_disk,
            )
            scopes = read_scopes_from_disk(self._runtime_root, spawn_id)
            for scope in scopes:
                mark_scope_released(self._runtime_root, spawn_id, scope.scope_id)
        return CompleteSpawnOutcome(
            wrote=outcome.wrote,
            transitioned=outcome.transitioned,
            entered_finalizing=entered_finalizing,
            already_terminal=was_terminal,
            snapshot=outcome.snapshot,
            spawn_id=spawn_id,
        )

    # ---- Archive Operations (SEAM-5) ----

    async def archive(self, spawn_id: SpawnId | str) -> bool:
        """Archive a terminal spawn. Emits spawn.archived.

        SEAM-5.1: Raises ValueError if spawn is not terminal.
        SEAM-5.2: Returns False if already archived (idempotent).
        SEAM-5.3: Emits spawn.archived exactly once.
        """
        from meridian.lib.spawn.archive import archive_spawn, is_spawn_archived

        spawn_id_str = str(spawn_id)
        lock = await self._locks.acquire(spawn_id_str)
        async with lock:
            record = self.require_spawn(spawn_id)

            # SEAM-5.1: Validate terminal state
            if not self.is_terminal(record.status):
                raise ValueError(
                    f"Cannot archive non-terminal spawn (status: {record.status}). "
                    "Wait for spawn to complete or cancel it first."
                )

            # SEAM-5.2/5.3: serialize check + write + event so exactly one
            # in-process caller observes not-yet-archived and emits.
            if is_spawn_archived(self._runtime_root, spawn_id_str):
                return False

            archive_spawn(self._runtime_root, spawn_id_str)

            notify_observers(
                LifecycleEvent(
                    event="spawn.archived",
                    spawn_id=spawn_id_str,
                    harness_id=record.harness or "",
                    model=record.model or "",
                    agent=record.agent,
                    ts=datetime.now(tz=UTC),
                    seq=next_spawn_sequence(spawn_id_str),
                    payload={"archived": True},
                )
            )

            return True

    # ---- Metadata Updates (SEAM-6) ----

    def update_metadata(
        self,
        spawn_id: SpawnId | str,
        *,
        execution_cwd: str | None = None,
        desc: str | None = None,
        work_id: str | None = None,
        harness_session_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update spawn metadata and emit spawn.updated.

        SEAM-6.1: Persists update via spawn_store and emits spawn.updated.
        SEAM-6.2: Does NOT transition lifecycle state.

        Delegates to spawn_store.update_spawn(). Emits lifecycle event
        so observers (SSE, WS, debug trace) see metadata changes.
        """
        # Only call store if at least one field is provided
        if all(
            v is None
            for v in (execution_cwd, desc, work_id, harness_session_id, error)
        ):
            return

        spawn_store.update_spawn(
            self._runtime_root,
            spawn_id,
            execution_cwd=execution_cwd,
            desc=desc,
            work_id=work_id,
            harness_session_id=harness_session_id,
            error=error,
        )


def _cancel_outcome_from_signal(
    spawn_id: str,
    outcome: SignalCancelOutcome,
    record: SpawnRecord,
) -> CancelOutcome:
    return CancelOutcome(
        spawn_id=spawn_id,
        status=outcome.status,
        origin=outcome.origin,
        exit_code=outcome.exit_code,
        already_terminal=outcome.already_terminal,
        finalizing=outcome.finalizing,
        model=record.model,
        harness=record.harness,
    )


def _cancel_outcome_from_record(
    spawn_id: str,
    record: SpawnRecord,
    *,
    already_terminal: bool = False,
    finalizing: bool = False,
) -> CancelOutcome:
    return CancelOutcome(
        spawn_id=spawn_id,
        status=_coerce_cancel_status(record.status),
        origin=record.terminal_origin or "cancel",
        exit_code=record.exit_code if record.exit_code is not None else 1,
        already_terminal=already_terminal,
        finalizing=finalizing,
        model=record.model,
        harness=record.harness,
    )


def _coerce_cancel_status(status: str) -> SpawnStatus:
    if status in {"queued", "running", "finalizing", "succeeded", "failed", "cancelled"}:
        return cast("SpawnStatus", status)
    return "failed"


def _is_managed_primary_candidate(record: SpawnRecord) -> bool:
    harness = (record.harness or "").strip().lower()
    return record.kind == "primary" and harness in {"codex", "opencode"}


def _started_at_epoch(started_at: str | None) -> float | None:
    normalized = (started_at or "").strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


__all__ = [
    "CancelOutcome",
    "CompleteSpawnOutcome",
    "KeyedLockRegistry",
    "PrepareSpawnRequest",
    "PreparedSpawn",
    "SpawnApplicationService",
]
