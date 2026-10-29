"""Shared spawn application service for all surfaces."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import structlog

from meridian.lib.catalog.model_aliases import MarsResultCache
from meridian.lib.core.clock import RealClock
from meridian.lib.core.domain import SpawnStatus, TerminalSpawnStatus, TokenUsage
from meridian.lib.core.lifecycle import SpawnLifecycleService
from meridian.lib.core.spawn_lifecycle import (
    CANCEL_GRACE_SECS,
    ExecutionTerminalFacts,
    ExecutionTerminalOutcome,
    SpawnReservation,
    coerce_spawn_status,
    is_active_spawn_status,
    is_terminal_spawn_status,
    resolve_execution_terminal_outcome,
)
from meridian.lib.core.spawn_start import SpawnStartMetadata
from meridian.lib.core.telemetry import (
    LifecycleObserver,
    LifecycleObserverTier,
    register_observer,
)
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.launch.artifact_io import write_projection_artifacts
from meridian.lib.launch.composition_spawn import (
    bind_spawn_launch_context,
    compose_spawn_launch_surface,
)
from meridian.lib.launch.context import (
    LaunchContext,
    RuntimeBindings,
    materialize_launch_context,
)
from meridian.lib.launch.env import apply_pi_bind_time_env, resolve_pi_session_role
from meridian.lib.launch.fork import fork_usage_seed
from meridian.lib.launch.request import LaunchRuntime, SpawnRequest
from meridian.lib.launch.resolve import (
    resolve_pi_child_wave_timeout_seconds,
    resolve_pi_task_ping_interval_seconds,
    resolve_resident_deadline_seconds,
    resolve_resident_poll_seconds,
)
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import spawn_store
from meridian.lib.state.json_doc import write_model_once
from meridian.lib.state.launch_lock import LaunchSlot
from meridian.lib.state.liveness import is_process_alive_with_birth, signal_with_birth
from meridian.lib.state.managed_primary import tear_down_dead_launch
from meridian.lib.state.paths import LaunchPaths, RuntimePaths
from meridian.lib.state.primary_meta import read_primary_metadata
from meridian.lib.state.spawn.model import (
    CancelIntent,
    LaunchMode,
    SpawnOrigin,
    SpawnRecord,
    TerminalFacts,
)
from meridian.lib.state.spawn.repository import Applied, Decline, Declined, write_state_locked
from meridian.lib.state.spawn_status import (
    POLL_INTERVAL_SECS,
    Claimed,
    Finished,
    Live,
    Lockless,
    Running,
    Settled,
    claim_dead_launch,
    derive,
    legacy_runner_alive,
    overlay_derived,
    poll_launch,
    read_cancel_intent,
    wait_until,
)
from meridian.lib.state.timestamps import iso_timestamp_to_epoch
from meridian.plugin_api.types import SpawnKind

if TYPE_CHECKING:
    from meridian.lib.harness.registry import HarnessRegistry
    from meridian.lib.observability.debug_tracer import DebugTracer
    from meridian.lib.state.primary_meta import PrimaryMetadata


_CANCEL_GRACE_SECS = CANCEL_GRACE_SECS
_CANCEL_KILL_SECS = 1.0


def _fold_cancel_intent_into_facts(
    facts: ExecutionTerminalFacts,
    intent: CancelIntent | None,
) -> ExecutionTerminalFacts:
    """Treat a durable cancel request as observed cancellation evidence.

    The runner may not have observed the signal that ``spawn cancel`` delivered,
    so the intent itself is cancellation evidence. Folding it into the execution
    facts lets the single lifecycle precedence resolver decide, instead of a
    second rule overriding its result.
    """

    if intent is None:
        return facts
    return replace(
        facts,
        cancellation_observed=True,
        failure_reason=facts.failure_reason or intent.error,
    )


def _is_live_managed_primary(
    runtime_root: Path, record: SpawnRecord, metadata: PrimaryMetadata | None
) -> bool:
    if record.kind != "primary" or record.chat_id is None:
        return False
    from meridian.lib.state.session_store import is_session_lease_owner_alive

    if is_session_lease_owner_alive(runtime_root, str(record.chat_id)):
        return True
    return bool(
        metadata is not None
        and metadata.managed_backend
        and metadata.launcher_pid is not None
        and is_process_alive_with_birth(
            metadata.launcher_pid,
            metadata.launcher_birth_epoch,
        )
    )


def _live_primary_cancel_refusal(record: SpawnRecord) -> str:
    agent = (record.agent or "unknown").strip() or "unknown"
    chat_id = str(record.chat_id) if record.chat_id is not None else "unknown"
    uptime = "unknown"
    if record.started_at is not None:
        started_epoch = iso_timestamp_to_epoch(record.started_at)
        if started_epoch is not None:
            elapsed = max(0, int(time.time() - started_epoch))
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime = f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"
    return (
        f"Refusing to cancel live managed primary '{record.id}' "
        f"(agent: {agent}, chat: {chat_id}, uptime: {uptime}). "
        f"To end this interactive session anyway, pass --force: "
        f"meridian spawn cancel {record.id} --force"
    )


_MAX_REAP_PASSES = 4
logger = structlog.get_logger()


def _config_snapshot_env(config_snapshot: dict[str, object]) -> dict[str, str]:
    env = config_snapshot.get("env")
    if isinstance(env, dict):
        result: dict[str, str] = {}
        for k, v in cast("dict[str, object]", env).items():
            if not k.strip():
                continue
            if not isinstance(v, str):
                continue
            result[k] = v
        return result
    return {}


def _resolve_config_snapshot(
    launch_context: object,
    *,
    fallback: dict[str, object],
) -> dict[str, object]:
    """Extract launch config snapshot with fallback for test doubles."""

    runtime = getattr(launch_context, "runtime", None)
    config_snapshot = getattr(runtime, "config_snapshot", None)
    if isinstance(config_snapshot, dict):
        return cast("dict[str, object]", config_snapshot)
    return fallback


@dataclass(frozen=True)
class CancelOutcome:
    """Surface-neutral result of cancelling a spawn."""

    spawn_id: str
    status: SpawnStatus
    origin: SpawnOrigin
    exit_code: int
    # The launch this outcome is for: the one the cancel observed, not necessarily the current.
    launch_number: int
    already_terminal: bool = False
    model: str | None = None
    harness: str | None = None


@dataclass(frozen=True)
class CompleteSpawnOutcome:
    """Surface-neutral result of a spawn finalization attempt."""

    wrote: bool
    transitioned: bool
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
    launch: LaunchPaths
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
    kind: SpawnKind = "child"
    desc: str | None = None
    work_id: str | None = None
    launch_mode: LaunchMode | None = None
    runner_pid: int | None = None
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
    ) -> None:
        self._runtime_root = runtime_root
        self._lifecycle = lifecycle_service
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
        return is_terminal_spawn_status(status)

    # ---- Spawn Preparation (SEAM-1, SEAM-2, SEAM-3) ----

    async def prepare(
        self, payload: PrepareSpawnRequest, *, launch_lock: LaunchSlot
    ) -> PreparedSpawn:
        """Resolve launch context, create spawn row, and project ConnectionConfig.

        SEAM-1: No spawn row is created until launch preparation succeeds.
        SEAM-2: Row metadata always reflects resolved values (never "unknown").
        SEAM-3: ConnectionConfig is projected from LaunchContext.
        SEAM-ID.1: ID allocation happens atomically with row creation.

        Raises on resolution failure, with no spawn row. If writing the launch files
        (``system-prompt.md``) fails after the row is persisted, the row stays
        ``running`` until the reaper finalizes it.
        On success, row exists with resolved metadata.
        """
        mars_cache = MarsResultCache()
        prepared_surface = await asyncio.to_thread(
            compose_spawn_launch_surface,
            request=payload.request,
            runtime=payload.runtime,
            harness_registry=payload.harness_registry,
            dry_run=False,
            runtime_work_id=(payload.work_id or "").strip() or None,
            cache=mars_cache,
        )

        resolved_request = prepared_surface.request
        resolved_model = (resolved_request.model or "").strip()
        resolved_harness = (resolved_request.harness or "").strip()
        resolved_agent = (resolved_request.agent or "").strip() or None

        if not resolved_harness:
            raise ValueError("Harness resolution failed - harness is required")

        effective_work_id = (payload.work_id or "").strip() or None

        reserved_launch = await asyncio.to_thread(
            spawn_store.reserve_launch,
            self._runtime_root,
        )
        final_spawn_id = reserved_launch.spawn_id
        config_env = _config_snapshot_env(payload.runtime.config_snapshot)
        request_env = dict(prepared_surface.request.env)
        resolved_env = {**config_env, **request_env}
        launch_ctx = await asyncio.to_thread(
            bind_spawn_launch_context,
            prepared=prepared_surface,
            bindings=RuntimeBindings(
                launch=reserved_launch,
                runtime_work_id=effective_work_id,
                dry_run=False,
                plan_overrides=resolved_env,
            ),
            runtime=payload.runtime,
            harness_registry=payload.harness_registry,
        )
        launch_ctx = await asyncio.to_thread(materialize_launch_context, launch_ctx)
        resolved_request = launch_ctx.resolved_request
        resolved_model = (resolved_request.model or "").strip()
        resolved_harness = (resolved_request.harness or "").strip()
        resolved_agent = (resolved_request.agent or "").strip() or None
        if not resolved_harness:
            raise ValueError("Harness resolution failed - harness is required")
        effective_work_id = (payload.work_id or launch_ctx.work_id or "").strip() or None
        start_metadata = SpawnStartMetadata(
            forked_from_history_id=resolved_request.session.forked_from_history_id,
            usage_seed=fork_usage_seed(self._runtime_root, resolved_request.session),
            desc=payload.desc,
            work_id=effective_work_id,
            goal=getattr(resolved_request, "goal", None),
            launch_policy_snapshot=resolved_request.launch_policy_snapshot,
        )

        # SEAM-ID.1: Persist the already-reserved ID via lifecycle service only
        # after launch context composition succeeds. Failed composition leaves no
        # spawn row and emits no spawn.created hook/telemetry.
        prepare_session_metadata = PrimarySessionMetadata(
            harness=resolved_harness,
            model=resolved_model,
            agent=resolved_agent or "",
            agent_path=resolved_request.agent_metadata.get("session_agent_path") or "",
            skills=resolved_request.skills,
            skill_paths=resolved_request.skill_paths,
        )
        published_launch = await asyncio.to_thread(
            self._lifecycle.start,
            SpawnReservation(
                chat_id=payload.chat_id or "",
                owner_chat_id=payload.chat_id or "",
                parent_id=payload.parent_id,
                session_metadata=prepare_session_metadata,
                kind=payload.kind,
                prompt=resolved_request.prompt,
                metadata=start_metadata,
                spawn_id=str(final_spawn_id),
                harness_session_id=resolved_request.session.requested_harness_session_id,
                control_root=launch_ctx.control_root.as_posix(),
                task_cwd=(
                    launch_ctx.task_cwd.as_posix() if launch_ctx.task_cwd is not None else None
                ),
                execution_cwd=launch_ctx.binding.child_cwd.as_posix(),
                launch_mode=payload.launch_mode,
                runner_pid=payload.runner_pid,
                launch_policy_snapshot=resolved_request.launch_policy_snapshot,
            ),
            launch_lock=launch_lock,
        )
        if published_launch != reserved_launch:
            raise RuntimeError(
                f"Reserved launch {reserved_launch} but published {published_launch}"
            )
        # The spawn dir exists only now, and the bound launch may reference files in it
        # (``system-prompt.md``), so a prepared spawn is launchable without caller help.
        launch = published_launch
        write_projection_artifacts(
            launch=launch,
            launch_context=launch_ctx,
            surface="spawn",
        )

        # SEAM-3: Project ConnectionConfig from LaunchContext
        harness_id = HarnessId(resolved_harness)
        pi_session_role = (
            resolve_pi_session_role(interactive=launch_ctx.binding.run_params.interactive)
            if harness_id is HarnessId.PI
            else None
        )
        launch_config_snapshot = _resolve_config_snapshot(
            launch_ctx,
            fallback=payload.runtime.config_snapshot,
        )
        child_cwd = launch_ctx.binding.child_cwd
        connection_task_cwd = (
            child_cwd if child_cwd.resolve() != launch_ctx.control_root.resolve() else None
        )
        child_env = dict(launch_ctx.binding.environment.final_env)
        pi_child_wave_timeout_seconds = resolve_pi_child_wave_timeout_seconds(
            explicit_timeout_seconds=None,
            config_snapshot=launch_config_snapshot,
        )
        pi_task_ping_interval_seconds = resolve_pi_task_ping_interval_seconds(
            explicit_interval_seconds=resolved_request.pi_task_ping_interval_seconds,
            config_snapshot=launch_config_snapshot,
        )
        if harness_id is HarnessId.PI:
            assert pi_session_role is not None
            apply_pi_bind_time_env(
                child_env,
                launch_role=pi_session_role,
                timeout_seconds=pi_child_wave_timeout_seconds,
                interval_seconds=pi_task_ping_interval_seconds,
                reset_on_activity=resolved_request.pi_task_ping_reset_on_activity,
            )
        connection_config = ConnectionConfig(
            harness_id=harness_id,
            prompt=launch_ctx.binding.run_params.prompt,
            control_root=launch_ctx.control_root,
            child_env=child_env,
            launch=launch,
            task_cwd=connection_task_cwd,
            pi_child_wave_timeout_seconds=pi_child_wave_timeout_seconds,
            resident_deadline_seconds=resolve_resident_deadline_seconds(
                config_snapshot=launch_config_snapshot,
                harness_id=os.getenv("_MERIDIAN_HARNESS") or harness_id,
            ),
            resident_poll_seconds=resolve_resident_poll_seconds(
                config_snapshot=launch_config_snapshot,
            ),
            resident_rearm_budget=resolved_request.execution_policy.resident_rearm_budget,
            pi_task_ping_interval_seconds=pi_task_ping_interval_seconds,
            pi_task_ping_reset_on_activity=resolved_request.pi_task_ping_reset_on_activity,
            pi_session_role=pi_session_role,
            debug_tracer=payload.debug_tracer,
        )

        return PreparedSpawn(
            spawn_id=final_spawn_id,
            launch=launch,
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
        kind: SpawnKind = "child",
        desc: str | None = None,
        work_id: str | None = None,
        launch_mode: LaunchMode | None = None,
        runner_pid: int | None = None,
        debug_tracer: DebugTracer | None = None,
        launch_lock: LaunchSlot,
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
                debug_tracer=debug_tracer,
            ),
            launch_lock=launch_lock,
        )

    # ---- Spawn Operations ----

    async def cancel(
        self,
        spawn_id: SpawnId,
        *,
        requested_by: Literal["user", "system"] = "user",
        force: bool = False,
    ) -> CancelOutcome:
        """Cancel a spawn: observe once, record the intent, claim a dead launch, else signal.

        One deadline covers every wait: ``_CANCEL_GRACE_SECS`` for the runner to stop on
        SIGTERM, then ``_CANCEL_KILL_SECS`` after SIGKILL.
        """
        grace_end = time.monotonic() + _CANCEL_GRACE_SECS
        deadline = grace_end + _CANCEL_KILL_SECS

        # Observe once. The stored row is read, not the derived one: a dead launch with no
        # terminal yet reads as terminal when derived, and the claim below must write it.
        # Nothing after this re-reads the row by ID.
        row = self.require_spawn(spawn_id)
        if self.is_terminal(row.status):
            self._cleanup_orphan_managed_primary(spawn_id, row)
            return _cancel_outcome_from_record(str(spawn_id), row, already_terminal=True)
        launch = LaunchPaths.for_row(self._runtime_root, row)
        primary_metadata = read_primary_metadata(launch)
        if not force and _is_live_managed_primary(self._runtime_root, row, primary_metadata):
            raise RuntimeError(_live_primary_cancel_refusal(row))

        await asyncio.to_thread(
            self._record_cancel_intent,
            row,
            launch,
            CancelIntent(
                requested_at=RealClock().utc_now_iso(),
                exit_code=130,
                error="cancelled",
                requested_by=requested_by,
            ),
        )

        # A launch whose runner is already dead ends here, with no signal and no grace.
        ended = await self._end_dead_launch(row, primary_metadata, deadline)
        if ended is None:
            signal_with_birth(row.runner_pid, row.runner_created_at_epoch, signal.SIGTERM)
            ended = await self._await_end(row, primary_metadata, until=grace_end, deadline=deadline)
        if ended is None:
            signal_with_birth(
                row.runner_pid, row.runner_created_at_epoch, signal.SIGKILL, tree=True
            )
            if primary_metadata is not None and primary_metadata.managed_backend:
                for pid, birth in (
                    (primary_metadata.backend_pid, primary_metadata.backend_birth_epoch),
                    (primary_metadata.tui_pid, primary_metadata.tui_birth_epoch),
                ):
                    signal_with_birth(pid, birth, signal.SIGKILL)
            ended = await self._await_end(row, primary_metadata, until=deadline, deadline=deadline)
        if ended is not None:
            return _cancel_outcome_from_record(str(spawn_id), ended)
        # Still held at the deadline (a process stuck in the kernel, or a brief holder).
        # The intent stays in the launch's directory for whoever ends the launch.
        return replace(
            _cancel_outcome_from_record(str(spawn_id), row),
            status=SpawnStatus.FINALIZING,
            exit_code=1,
        )

    def _record_cancel_intent(
        self, observed: SpawnRecord, launch: LaunchPaths, intent: CancelIntent
    ) -> None:
        """First request wins: ``cancel.json`` in the observed launch's directory.

        A v3 row (``launch`` absent) may be run by an older build, which decides ``cancelled``
        only from the row's ``cancel_intent``, so that row gets the field too. The mutator
        re-checks ``launch`` inside the write: a reopen sets it under the launch lock, so the
        field can only land on the v3 launch it was meant for. A v4 row never gets it.
        """
        write_model_once(launch.cancel, intent)
        if observed.launch is not None:
            return

        def mutate(current: SpawnRecord) -> SpawnRecord | Decline:
            if (
                current.launch is not None
                or not is_active_spawn_status(current.status)
                or current.cancel_intent is not None
            ):
                return Decline("no cancel intent to record")
            return current.model_copy(update={"cancel_intent": intent})

        write_state_locked(
            RuntimePaths.from_root_dir(self._runtime_root).spawns_dir, launch.spawn_id, mutate
        )

    async def _await_end(
        self,
        row: SpawnRecord,
        primary_metadata: PrimaryMetadata | None,
        *,
        until: float,
        deadline: float,
    ) -> SpawnRecord | None:
        """Wait for the launch's outcome, claiming it when its runner is gone."""
        while True:
            poll = await wait_until(self._runtime_root, row.id, row.launch_number, until=until)
            if isinstance(poll, Finished):
                return poll.row
            if isinstance(poll, Running):
                return None
            ended = await self._end_dead_launch(row, primary_metadata, deadline)
            if ended is not None:
                return ended
            # Someone else took the lock between the poll and the claim.
            if time.monotonic() >= until:
                return None
            await asyncio.sleep(POLL_INTERVAL_SECS)

    async def _end_dead_launch(
        self,
        row: SpawnRecord,
        primary_metadata: PrimaryMetadata | None,
        deadline: float,
    ) -> SpawnRecord | None:
        """Claim a launch without a runner, tear down what it left and write its outcome.

        Returns the launch's terminal row, or ``None`` while a runner (or a brief holder)
        still holds the launch lock. ``deadline`` (monotonic) bounds the teardown.
        """
        spawn_id = SpawnId(row.id)

        def sweep(stored: SpawnRecord) -> None:
            tear_down_dead_launch(self._runtime_root, stored, primary_metadata, until=deadline)

        lock = await self._locks.acquire(row.id)
        async with lock:
            ended = await self._finalize_dead_launch_unlocked(
                spawn_id,
                before_finalize=sweep,
                scopes_torn_down=True,
                expected_n=row.launch_number,
            )
            if isinstance(ended, SpawnRecord):
                return ended
            if isinstance(ended, Lockless) and not legacy_runner_alive(row):
                # A row from before launch locks: its runner is gone when its PID is.
                await asyncio.to_thread(sweep, row)
                derived = derive(self._runtime_root, row)
                terminal = cast("TerminalFacts", derived.terminal)
                outcome = await self._complete_spawn_unlocked(
                    spawn_id,
                    cast("TerminalSpawnStatus", derived.status),
                    terminal.exit_code,
                    origin=terminal.origin,
                    error=terminal.error,
                    expected_n=row.launch_number,
                    finished_at=terminal.finished_at,
                )
                snapshot = outcome.snapshot
                if snapshot is None or snapshot.launch_number == row.launch_number:
                    return snapshot
                # A continue converted the row and started the next launch during the sweep:
                # the finish was declined, and this launch's outcome is in its archive.
                return poll_launch(self._runtime_root, row.id, row.launch_number).row
            return None

    async def cancel_descendants(self, root_id: SpawnId | str) -> set[str]:
        """Cancel the active descendant subtree of a spawn, to a fixed point.

        Each descendant goes through the full cancel sequence (intent, claim, signal,
        wait), so a child whose runner dies without self-finalizing is still driven to
        a terminal status rather than left as an orphan row. Rescans after each pass to
        catch descendants spawned during the reap; bounded by ``_MAX_REAP_PASSES`` so a
        wedged descendant cannot loop forever.
        """
        from meridian.lib.state.spawn_tree import active_descendants

        reaped_ids: set[str] = set()
        for _ in range(_MAX_REAP_PASSES):
            descendants = active_descendants(self._runtime_root, root_id)
            if not descendants:
                return reaped_ids
            results = await asyncio.gather(
                *(
                    self.cancel(SpawnId(d.id), requested_by="system", force=False)
                    for d in descendants
                ),
                return_exceptions=True,
            )
            for descendant, result in zip(descendants, results, strict=True):
                if isinstance(result, BaseException):
                    logger.warning(
                        "Descendant reap cancel raised.",
                        root_id=str(root_id),
                        descendant_id=descendant.id,
                        error=repr(result),
                    )
                    continue
                if self.is_terminal(result.status):
                    reaped_ids.add(descendant.id)
        remaining = active_descendants(self._runtime_root, root_id)
        remaining = [
            row
            for row in overlay_derived(self._runtime_root, remaining)
            if is_active_spawn_status(row.status)
        ]
        if remaining:
            logger.warning(
                "Descendant reap did not converge; descendants still active.",
                root_id=str(root_id),
                remaining=[d.id for d in remaining],
            )
        return reaped_ids

    async def finalize_dead_launch(
        self,
        spawn_id: SpawnId,
        *,
        before_finalize: Callable[[SpawnRecord], None] | None = None,
    ) -> SpawnRecord | Live | Lockless:
        """Claim and persist one dead lock-backed launch under both service locks."""

        lock = await self._locks.acquire(str(spawn_id))
        async with lock:
            return await self._finalize_dead_launch_unlocked(
                spawn_id, before_finalize=before_finalize
            )

    async def _finalize_dead_launch_unlocked(
        self,
        spawn_id: SpawnId,
        *,
        before_finalize: Callable[[SpawnRecord], None] | None = None,
        scopes_torn_down: bool = False,
        expected_n: int | None = None,
    ) -> SpawnRecord | Live | Lockless:
        claimed = await asyncio.to_thread(
            partial(claim_dead_launch, self._runtime_root, str(spawn_id), expected_n=expected_n)
        )
        if isinstance(claimed, Settled):
            claimed.handle.close()
            return claimed.stored
        if not isinstance(claimed, Claimed):
            return claimed

        with claimed.handle:
            if before_finalize is not None:
                await asyncio.to_thread(before_finalize, claimed.stored)

            from meridian.lib.state.managed_primary import is_managed_primary_candidate

            outcome = await self._complete_spawn_unlocked(
                spawn_id,
                claimed.status,
                claimed.terminal.exit_code,
                origin=claimed.terminal.origin,
                error=claimed.terminal.error,
                managed_scopes_pending=(
                    not scopes_torn_down and is_managed_primary_candidate(claimed.stored)
                ),
                finished_at=claimed.terminal.finished_at,
            )
            snapshot = outcome.snapshot or claimed.stored
            if outcome.wrote:
                logger.debug(
                    "Finalized active spawn after acquiring its free launch lock.",
                    spawn_id=str(spawn_id),
                    status=claimed.status,
                    error=claimed.terminal.error,
                    origin=claimed.terminal.origin,
                )
            return snapshot

    def _cleanup_orphan_managed_primary(
        self,
        spawn_id: SpawnId,
        record: SpawnRecord,
    ) -> None:
        """Best-effort cleanup for terminal managed orphan-primary spawns."""
        if record.terminal is None or not record.terminal.managed_scopes_pending:
            return
        from meridian.lib.state.managed_primary import release_managed_primary_scopes

        release_managed_primary_scopes(self._runtime_root, spawn_id, record)

    async def complete_spawn(
        self,
        spawn_id: SpawnId,
        status: TerminalSpawnStatus,
        exit_code: int,
        *,
        origin: SpawnOrigin,
        duration_secs: float | None = None,
        usage: TokenUsage | None = None,
        error: str | None = None,
        managed_scopes_pending: bool = False,
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
                usage=usage,
                error=error,
                managed_scopes_pending=managed_scopes_pending,
            )

    async def complete_execution(
        self,
        spawn_id: SpawnId,
        facts: ExecutionTerminalFacts,
        *,
        origin: SpawnOrigin,
        duration_secs: float | None = None,
        usage: TokenUsage | None = None,
    ) -> CompleteExecutionOutcome:
        """Resolve execution facts, then finalize through the lifecycle authority."""

        lock = await self._locks.acquire(str(spawn_id))
        async with lock:
            record = self.get_spawn(spawn_id)
            intent = (
                None
                if record is None
                else read_cancel_intent(LaunchPaths.for_row(self._runtime_root, record), record)
            )
            resolved = resolve_execution_terminal_outcome(
                _fold_cancel_intent_into_facts(facts, intent)
            )
            completion = await self._complete_spawn_unlocked(
                spawn_id,
                resolved.status,
                resolved.exit_code,
                origin=origin,
                duration_secs=duration_secs,
                usage=usage,
                error=resolved.error,
            )
        return CompleteExecutionOutcome(
            resolved=resolved,
            completion=completion,
        )

    async def _complete_spawn_unlocked(
        self,
        spawn_id: SpawnId,
        status: TerminalSpawnStatus,
        exit_code: int,
        *,
        origin: SpawnOrigin,
        duration_secs: float | None = None,
        usage: TokenUsage | None = None,
        error: str | None = None,
        managed_scopes_pending: bool = False,
        expected_n: int | None = None,
        finished_at: str | None = None,
    ) -> CompleteSpawnOutcome:
        record = self.get_spawn(spawn_id)
        if record is None:
            return CompleteSpawnOutcome(
                wrote=False,
                transitioned=False,
                already_terminal=False,
                snapshot=None,
                spawn_id=spawn_id,
            )
        was_terminal = self.is_terminal(record.status)

        outcome = self._lifecycle.finalize(
            str(spawn_id),
            status,
            exit_code,
            origin=origin,
            duration_secs=duration_secs,
            usage=usage,
            error=error,
            managed_scopes_pending=managed_scopes_pending,
            expected_n=expected_n,
            finished_at=finished_at,
        )
        if isinstance(outcome, Applied):
            from meridian.lib.state.process_scope_projection import (
                mark_scope_released,
                read_scopes_from_disk,
            )

            launch = LaunchPaths.for_row(self._runtime_root, record)
            for scope in read_scopes_from_disk(launch):
                if scope.owner_policy == "spawn_owned":
                    mark_scope_released(launch, scope.release_id)
        return CompleteSpawnOutcome(
            wrote=isinstance(outcome, Applied),
            transitioned=(
                isinstance(outcome, Applied)
                and not self.is_terminal(outcome.before.status)
                and self.is_terminal(outcome.after.status)
            ),
            already_terminal=was_terminal,
            snapshot=(
                outcome.after
                if isinstance(outcome, Applied)
                else outcome.snapshot
                if isinstance(outcome, Declined)
                else None
            ),
            spawn_id=spawn_id,
        )


def _cancel_outcome_from_record(
    spawn_id: str,
    record: SpawnRecord,
    *,
    already_terminal: bool = False,
) -> CancelOutcome:
    terminal = record.terminal
    if terminal is not None:
        origin = terminal.origin
        exit_code = terminal.exit_code
    else:
        origin = "cancel"
        exit_code = record.cancel_intent.exit_code if record.cancel_intent is not None else 130
    return CancelOutcome(
        spawn_id=spawn_id,
        status=_coerce_cancel_status(record.status),
        origin=origin,
        exit_code=exit_code,
        already_terminal=already_terminal,
        model=record.model,
        harness=record.harness,
        launch_number=record.launch_number,
    )


def _coerce_cancel_status(status: str) -> SpawnStatus:
    return coerce_spawn_status(status)


__all__ = [
    "CancelOutcome",
    "CompleteSpawnOutcome",
    "KeyedLockRegistry",
    "PrepareSpawnRequest",
    "PreparedSpawn",
    "SpawnApplicationService",
]
