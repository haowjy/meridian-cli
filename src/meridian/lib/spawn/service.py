from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import structlog

from meridian.lib.core.clock import RealClock
from meridian.lib.core.domain import (
    CANCEL_GRACE_SECS,
    SpawnStatus,
    TerminalSpawnStatus,
    TokenUsage,
    coerce_spawn_status,
    is_active_spawn_status,
    is_terminal_spawn_status,
)
from meridian.lib.core.refs import RefNotFound
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import BootstrapMode
from meridian.lib.harness.plugin_hosted import is_plugin_hosted
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.platform.signals import signal_to_exit_code
from meridian.lib.spawn.cleanup import sweep_unreleased_scopes
from meridian.lib.spawn.lifecycle import SpawnLifecycleService
from meridian.lib.spawn.managed_primary import (
    release_managed_primary_scopes,
    require_managed_processes_gone,
    tear_down_dead_launch,
    terminate_managed_primary_processes,
)
from meridian.lib.spawn.outcome import (
    ExecutionTerminalFacts,
    ExecutionTerminalOutcome,
    resolve_execution_terminal_outcome,
)
from meridian.lib.spawn.reaper import resolve_cleanup_claim
from meridian.lib.state import spawn_store
from meridian.lib.state.json_doc import write_model_once
from meridian.lib.state.launch_lock import LaunchHandle, NotFound
from meridian.lib.state.liveness import is_process_alive_with_birth, signal_with_birth
from meridian.lib.state.paths import LaunchPaths, RuntimePaths
from meridian.lib.state.primary_meta import read_primary_metadata
from meridian.lib.state.process_scope_projection import (
    mark_scope_released,
    read_scopes_from_disk,
)
from meridian.lib.state.reaper_cleanup_claim import read_cleanup_claim
from meridian.lib.state.session_store import is_session_lease_owner_alive
from meridian.lib.state.spawn.model import (
    CancelIntent,
    SpawnOrigin,
    SpawnRecord,
    TerminalFacts,
)
from meridian.lib.state.spawn.repository import (
    Applied,
    Decline,
    Declined,
    read_state,
    write_state_locked,
)
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
from meridian.lib.state.spawn_tree import active_descendants
from meridian.lib.state.timestamps import iso_timestamp_to_epoch
from meridian.lib.telemetry.observers import (
    LifecycleObserver,
    LifecycleObserverTier,
    register_observer,
)

if TYPE_CHECKING:
    from meridian.lib.state.primary_meta import PrimaryMetadata


def _runs_managed_primary(record: SpawnRecord) -> bool:
    """Whether a row's harness runs a managed primary: the fallback when launch metadata is gone."""

    if record.kind != "primary":
        return False
    adapter = get_default_harness_registry().find((record.harness or "").strip().lower())
    return (
        adapter is not None
        and adapter.contract.bootstrap_mode is BootstrapMode.MANAGED_PRIMARY_ATTACH
    )


_CANCEL_GRACE_SECS = CANCEL_GRACE_SECS
_CANCEL_KILL_SECS = 1.0


def _fold_cancel_intent_into_facts(
    facts: ExecutionTerminalFacts,
    intent: CancelIntent | None,
) -> ExecutionTerminalFacts:
    # The runner may not observe the signal; durable intent is cancellation evidence.
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


class SpawnApplicationService:
    def __init__(
        self,
        runtime_root: Path,
        lifecycle_service: SpawnLifecycleService,
    ) -> None:
        self._runtime_root = runtime_root
        self._lifecycle = lifecycle_service

    @property
    def runtime_root(self) -> Path:
        return self._runtime_root

    @property
    def lifecycle(self) -> SpawnLifecycleService:
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

    def get_spawn(self, spawn_id: SpawnId | str) -> SpawnRecord | None:
        return spawn_store.get_spawn(self._runtime_root, spawn_id)

    def require_spawn(self, spawn_id: SpawnId | str) -> SpawnRecord:
        record = self.get_spawn(spawn_id)
        if record is None:
            raise RefNotFound(str(spawn_id), "spawn")
        return record

    def is_terminal(self, status: str) -> bool:
        return is_terminal_spawn_status(status)

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
        if is_plugin_hosted(row.harness):
            # Its server holds every conversation of one agy process, and no signal
            # Meridian may send stops agy itself, so force does not lift this.
            raise RuntimeError(f"this session runs in {row.harness}; end it there")
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
                exit_code=signal_to_exit_code(signal.SIGTERM),
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
        spawn_id = SpawnId(row.id)

        def sweep(stored: SpawnRecord) -> None:
            tear_down_dead_launch(
                self._runtime_root,
                stored,
                primary_metadata,
                managed_if_unrecorded=_runs_managed_primary(stored),
                until=deadline,
            )

        ended = await self.finalize_dead_launch(
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
            outcome = await self.complete_spawn(
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
        scopes_torn_down: bool = False,
        expected_n: int | None = None,
    ) -> SpawnRecord | Live | Lockless:
        """Claim and persist one dead lock-backed launch."""

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

            outcome = await self.complete_spawn(
                spawn_id,
                claimed.status,
                claimed.terminal.exit_code,
                origin=claimed.terminal.origin,
                error=claimed.terminal.error,
                managed_scopes_pending=(
                    not scopes_torn_down and _runs_managed_primary(claimed.stored)
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

        release_managed_primary_scopes(
            self._runtime_root,
            spawn_id,
            record,
            managed_if_unrecorded=_runs_managed_primary(record),
        )

    def finish_last_launch(self, handle: LaunchHandle) -> SpawnRecord:
        """Before a continue: end the last launch of the spawn whose launch lock ``handle`` holds.

        With the lock held no runner of the last launch is alive. Unlike ``finalize_dead_launch``,
        this writes the outcome (and dispatches ``spawn.finished``) before it stops what the
        launch left; ``.context/CONTEXT.md`` says why both orders exist.
        """
        spawn_id = str(handle.spawn_id)
        spawns_dir = self._runtime_root / "spawns"
        row = read_state(spawns_dir, spawn_id, include_prompt=False)
        if row is None:
            raise NotFound(f"Spawn '{spawn_id}' not found")
        launch = LaunchPaths.for_row(self._runtime_root, row)
        if row.terminal is None:
            derived = derive(self._runtime_root, row)
            terminal = derived.terminal
            assert terminal is not None
            outcome = self._lifecycle.finalize(
                spawn_id,
                cast("spawn_store.TerminalSpawnStatus", derived.status),
                terminal.exit_code,
                origin=terminal.origin,
                error=terminal.error,
                finished_at=terminal.finished_at,
                expected_n=row.launch_number,
            )
            if not isinstance(outcome, Applied):
                raise ValueError(f"{spawn_id} changed while it was being continued; retry")
            row = outcome.after
        # A claim records processes of the last launch whatever ended it; none may reach the next.
        if read_cleanup_claim(self._runtime_root, spawn_id):
            resolve_cleanup_claim(self._runtime_root, row)
            if read_cleanup_claim(self._runtime_root, spawn_id):
                raise ValueError(
                    f"{spawn_id} has processes from its last run that could not be stopped; "
                    "run `meridian doctor`"
                )
        # A managed primary's backend and TUI belong to its session, whose runner is dead.
        metadata = read_primary_metadata(launch)
        terminate_managed_primary_processes(metadata, include_launcher=False)
        sweep_unreleased_scopes(launch, spawn_id)
        require_managed_processes_gone(metadata, spawn_id)
        return row

    async def complete_execution(
        self,
        spawn_id: SpawnId,
        facts: ExecutionTerminalFacts,
        *,
        origin: SpawnOrigin,
        duration_secs: float | None = None,
        usage: TokenUsage | None = None,
    ) -> CompleteExecutionOutcome:

        record = self.get_spawn(spawn_id)
        intent = (
            None
            if record is None
            else read_cancel_intent(LaunchPaths.for_row(self._runtime_root, record), record)
        )
        resolved = resolve_execution_terminal_outcome(_fold_cancel_intent_into_facts(facts, intent))
        completion = await self.complete_spawn(
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
    "SpawnApplicationService",
]
