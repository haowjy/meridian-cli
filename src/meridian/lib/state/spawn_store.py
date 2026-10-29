"""File-backed spawn state store for a Meridian runtime root.

Also includes file-backed ID generation for spawns and sessions.
"""

from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from uuid import UUID, uuid4

import psutil
import structlog

from meridian.lib.core.clock import Clock, RealClock
from meridian.lib.core.domain import SpawnStatus, TokenUsage
from meridian.lib.core.launch_policy_snapshot import (
    LaunchPolicySnapshot as LaunchPolicySnapshot,
)
from meridian.lib.core.refs import is_spawn_id
from meridian.lib.core.spawn_lifecycle import (
    is_active_spawn_status as _is_active_spawn_status,
)
from meridian.lib.core.spawn_start import SpawnStartMetadata, derive_display_label
from meridian.lib.core.types import ChatId, HarnessSessionId, SpawnId
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_publish_dir, atomic_write_text
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.launch_lock import LaunchHandle, LaunchSlot
from meridian.lib.state.paths import (
    LaunchPaths,
    RuntimePaths,
    launch_archive,
    normalize_path_for_write,
    staged_starting_prompt,
)
from meridian.lib.state.spawn.model import (
    BACKGROUND_LAUNCH_MODE,
    LaunchRef,
    OwedLaunch,
)
from meridian.lib.state.spawn.model import (
    LaunchMode as LaunchMode,
)
from meridian.lib.state.spawn.model import RunBoundaryOutcome as RunBoundaryOutcome
from meridian.lib.state.spawn.model import (
    SpawnOrigin as SpawnOrigin,
)
from meridian.lib.state.spawn.model import (
    SpawnRecord as SpawnRecord,
)
from meridian.lib.state.spawn.model import (
    TerminalSpawnStatus as TerminalSpawnStatus,
)
from meridian.lib.state.spawn.repository import (
    Applied,
    Decline,
    LockedMutationResult,
    SpawnStateQuarantined,
    SpawnStateQuarantineReport,
    record_to_stored_state,
)
from meridian.lib.state.spawn.repository import (
    is_safe_spawn_dir_name as _is_safe_spawn_dir_name,
)
from meridian.lib.state.spawn.repository import read_state as _read_state
from meridian.lib.state.spawn.repository import scan_spawn_ids as _scan_spawn_ids
from meridian.lib.state.spawn.repository import write_state_locked as _write_state_locked
from meridian.lib.state.spawn.transitions import (
    apply_finalize,
    apply_launch_facts,
)
from meridian.lib.state.spawn_aggregate import (
    delete_published_spawn,
    ensure_spawn_staging_dir,
    sync_retirement_parents,
)
from meridian.plugin_api.types import SpawnKind

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# ID generation (absorbed from state/id_gen.py)
# ---------------------------------------------------------------------------


def _spawn_id_index(spawn_id: SpawnId | str) -> int:
    value = str(spawn_id)
    if is_spawn_id(value):
        return int(value[1:])
    return 0


def _spawn_counter_path(paths: RuntimePaths) -> Path:
    return paths.root_dir / "spawn-id-counter"


def read_spawn_counter(runtime_root: Path) -> int:
    """The last ID reserved from the counter every build shares (0 when unreadable)."""
    try:
        return int((runtime_root / "spawn-id-counter").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _seed_spawn_counter_from_dirs(paths: RuntimePaths) -> int:
    return max(
        (_spawn_id_index(spawn_id) for spawn_id in _scan_spawn_ids(paths.spawns_dir)),
        default=0,
    )


def reserve_launch(
    runtime_root: Path,
) -> LaunchPaths:
    """Reserve the next spawn ID (`p1`, `p2`, ...) and return its first launch.

    Reservation is persisted separately from spawn rows so callers can compose
    launch context with the final launch before writing lifecycle-visible events.
    Pass the result to ``start_spawn`` (via the reservation); the launch it publishes
    equals this one.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    with lock_file(paths.spawns_flock):
        current = read_spawn_counter(paths.root_dir)
        if current == 0 and not _spawn_counter_path(paths).is_file():
            current = _seed_spawn_counter_from_dirs(paths)
        next_value = current + 1
        atomic_write_text(_spawn_counter_path(paths), f"{next_value}\n")
        return LaunchPaths.reserved(runtime_root, SpawnId(f"p{next_value}"), FIRST_LAUNCH)


def _runner_created_at_epoch_for_pid(runner_pid: int | None) -> float | None:
    if runner_pid is None or runner_pid <= 0:
        return None
    try:
        return psutil.Process(runner_pid).create_time()
    except (psutil.Error, OSError, ValueError):
        return None


def _resolve_start_metadata(
    *,
    metadata: SpawnStartMetadata | None,
    desc: str | None,
    work_id: str | None,
    goal: str | None,
    launch_policy_snapshot: LaunchPolicySnapshot | None,
) -> SpawnStartMetadata:
    if metadata is None:
        return SpawnStartMetadata(
            desc=desc,
            work_id=work_id,
            goal=goal,
            launch_policy_snapshot=launch_policy_snapshot,
        )
    return SpawnStartMetadata(
        forked_from_history_id=metadata.forked_from_history_id,
        desc=metadata.desc if metadata.desc is not None else desc,
        work_id=metadata.work_id if metadata.work_id is not None else work_id,
        goal=metadata.goal if metadata.goal is not None else goal,
        launch_policy_snapshot=(
            metadata.launch_policy_snapshot
            if metadata.launch_policy_snapshot is not None
            else launch_policy_snapshot
        ),
    )


def gc_abandoned_stages(runtime_root: Path) -> None:
    """Best-effort cleanup of unpublished buffers and verified-retention retirement residue."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    staging_dir = paths.spawns_dir / ".staging"
    with lock_file(paths.spawns_flock):
        if staging_dir.is_symlink():
            with suppress(OSError):
                staging_dir.unlink()
            return
        if not staging_dir.is_dir():
            return
        try:
            entries = tuple(staging_dir.iterdir())
            # Snapshot entries first: every captured retirement must precede
            # the synchronization that makes it safe to discard after a crash.
            if any("-retired-" in entry.name for entry in entries):
                sync_retirement_parents(paths.spawns_dir)
        except OSError:
            return
        for entry in entries:
            with suppress(OSError):
                if not entry.is_symlink() and entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()


FIRST_LAUNCH = 1


@dataclass
class LaunchInput:
    """What one launch of a spawn is told at start; the same for a first launch and a reopen.

    Construction validates and resolves, so a bad input fails before any file is written:
    ``goal`` is stripped (``None`` means the caller keeps the row's goal) and
    ``runner_created_at_epoch`` is looked up from ``runner_pid`` when not given.

    No field has a default: ``start_spawn`` builds this from its own parameters, and a
    field it forgot to forward is then a type error, not a silent default. The status is
    not here: a creation chooses it, a reopen is always running.
    """

    chat_id: str | None
    prompt: str
    kind: SpawnKind
    owner_chat_id: str | None
    parent_id: str | None
    goal: str | None
    harness_session_id: str | None
    launch_mode: LaunchMode | None
    worker_pid: int | None
    runner_pid: int | None
    runner_created_at_epoch: float | None

    def __post_init__(self) -> None:
        if self.goal is not None:
            self.goal = self.goal.strip()
            if not self.goal:
                raise ValueError("--goal cannot be empty")
        if self.runner_created_at_epoch is None:
            self.runner_created_at_epoch = _runner_created_at_epoch_for_pid(self.runner_pid)


class _LaunchFields(TypedDict):
    session_instance_id: str | None
    parent_history_id: UUID | None
    owner_history_id: UUID | None
    forked_from_history_id: UUID | None
    chat_id: ChatId | None
    owner_chat_id: ChatId | None
    parent_id: str | None
    originating_bash_id: str | None
    kind: SpawnKind
    display_label: str | None
    harness_session_id: HarnessSessionId | None
    launch_mode: LaunchMode | None
    worker_pid: int | None
    runner_pid: int | None
    runner_created_at_epoch: float | None
    status: SpawnStatus
    prompt: str
    started_at: str
    current_chat_id: None
    run_boundary: None
    resident_rearm_count: int
    last_attempt_exited_at: None
    last_attempt_exit_code: None
    runner_exit: None
    cancel_intent: None
    terminal: None


def _launch_fields(
    runtime_root: Path,
    launch: LaunchInput,
    *,
    goal: str | None,
    desc: str | None,
    forked_from_history_id: UUID | None,
    started_at: str,
    status: SpawnStatus,
) -> _LaunchFields:
    """Every field of a row that one launch owns, or derives from who starts it.

    The one builder behind a new spawn's first launch and a reopen's next one: a field
    added here reaches both, and the reset list cannot drift from the creation list.
    Spawn-level fields (ID, history identity, work, harness, launch policy) stay with
    the caller; ``goal``, ``desc`` and ``forked_from_history_id`` are the row's effective
    values. A reopen overrides ``session_instance_id``: the owner chat's current one
    belongs to the launch that just ended, not to this launch's runner.
    """
    from meridian.lib.state.session_store import chat_record

    paths = RuntimePaths.from_root_dir(runtime_root)
    parent_id = launch.parent_id
    parent = _read_state(paths.spawns_dir, parent_id, include_prompt=False) if parent_id else None
    owner_chat = launch.owner_chat_id or launch.chat_id
    owner = chat_record(runtime_root, owner_chat) if owner_chat else None
    primary = launch.kind == "primary"
    return {
        "session_instance_id": owner.session_instance_id if owner and primary else None,
        "parent_history_id": parent.history_id if parent else None,
        "owner_history_id": owner.history_id if owner and not primary else None,
        "forked_from_history_id": forked_from_history_id
        or (owner.forked_from_history_id if owner and primary else None),
        "chat_id": ChatId(launch.chat_id) if launch.chat_id is not None else None,
        "owner_chat_id": ChatId(launch.owner_chat_id) if launch.owner_chat_id is not None else None,
        "parent_id": parent_id,
        "originating_bash_id": os.environ.get("_MERIDIAN_PI_BASH_ID") or None,
        "kind": launch.kind,
        "display_label": derive_display_label(goal=goal, desc=desc, prompt=launch.prompt),
        "harness_session_id": (
            HarnessSessionId(launch.harness_session_id)
            if launch.harness_session_id is not None
            else None
        ),
        "launch_mode": launch.launch_mode,
        "worker_pid": launch.worker_pid,
        "runner_pid": launch.runner_pid,
        "runner_created_at_epoch": launch.runner_created_at_epoch,
        "status": status,
        "prompt": launch.prompt,
        "started_at": started_at,
        "current_chat_id": None,
        "run_boundary": None,
        "resident_rearm_count": 0,
        "last_attempt_exited_at": None,
        "last_attempt_exit_code": None,
        "runner_exit": None,
        "cancel_intent": None,
        "terminal": None,
    }


def start_spawn(
    runtime_root: Path,
    *,
    chat_id: str | None,
    owner_chat_id: str | None = None,
    parent_id: str | None = None,
    model: str,
    agent: str,
    agent_path: str | None = None,
    skills: tuple[str, ...] = (),
    skill_paths: tuple[str, ...] = (),
    harness: str,
    kind: SpawnKind = "child",
    prompt: str,
    metadata: SpawnStartMetadata | None = None,
    desc: str | None = None,
    work_id: str | None = None,
    goal: str | None = None,
    spawn_id: SpawnId | str | None = None,
    harness_session_id: str | None = None,
    control_root: str | None = None,
    task_cwd: str | None = None,
    execution_cwd: str | None = None,
    claude_config_dir: str | None = None,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    runner_created_at_epoch: float | None = None,
    launch_policy_snapshot: LaunchPolicySnapshot | None = None,
    status: SpawnStatus = SpawnStatus.RUNNING,
    started_at: str | None = None,
    clock: Clock | None = None,
    launch_lock: LaunchSlot | None = None,
) -> LaunchPaths:
    """Create a spawn state directory and return the launch it published."""

    resolved_clock = clock or RealClock()
    paths = RuntimePaths.from_root_dir(runtime_root)
    started = started_at or resolved_clock.utc_now_iso()
    start_metadata = _resolve_start_metadata(
        metadata=metadata,
        desc=desc,
        work_id=work_id,
        goal=goal,
        launch_policy_snapshot=launch_policy_snapshot,
    )
    launch = LaunchInput(
        chat_id=chat_id,
        prompt=prompt,
        kind=kind,
        owner_chat_id=owner_chat_id,
        parent_id=parent_id,
        goal=start_metadata.goal,
        harness_session_id=harness_session_id,
        launch_mode=launch_mode,
        worker_pid=worker_pid,
        runner_pid=runner_pid,
        runner_created_at_epoch=runner_created_at_epoch,
    )
    resolved_launch_policy_snapshot = (
        launch_policy_snapshot or start_metadata.launch_policy_snapshot
    )

    changes = HistoryChanges(runtime_root)
    with lock_file(changes.mutation_lock, mode="shared"), lock_file(paths.spawns_flock):
        if spawn_id is not None:
            explicit_spawn_id = str(spawn_id)
            if not _is_safe_spawn_dir_name(explicit_spawn_id):
                raise ValueError(f"Invalid spawn ID: {explicit_spawn_id}")
            resolved_spawn_id = SpawnId(explicit_spawn_id)
        else:
            current = read_spawn_counter(paths.root_dir)
            if current == 0 and not _spawn_counter_path(paths).is_file():
                current = _seed_spawn_counter_from_dirs(paths)
            next_value = current + 1
            atomic_write_text(_spawn_counter_path(paths), f"{next_value}\n")
            resolved_spawn_id = SpawnId(f"p{next_value}")
        record = SpawnRecord(
            id=str(resolved_spawn_id),
            history_id=uuid4(),
            state_revision=1,
            launch=LaunchRef(n=FIRST_LAUNCH),
            model=model,
            agent=agent,
            agent_path=agent_path,
            skills=skills,
            skill_paths=skill_paths,
            harness=harness,
            desc=start_metadata.desc,
            work_id=(
                start_metadata.work_id.strip() or None
                if start_metadata.work_id is not None
                else None
            ),
            goal=launch.goal,
            control_root=normalize_path_for_write(control_root),
            task_cwd=normalize_path_for_write(task_cwd),
            execution_cwd=normalize_path_for_write(execution_cwd),
            claude_config_dir=claude_config_dir,
            launch_policy_snapshot=resolved_launch_policy_snapshot,
            **_launch_fields(
                runtime_root,
                launch,
                goal=launch.goal,
                desc=start_metadata.desc,
                forked_from_history_id=start_metadata.forked_from_history_id,
                started_at=started,
                status=status,
            ),
        )
        spawn_dir = paths.spawns_dir / str(resolved_spawn_id)
        staging_dir = ensure_spawn_staging_dir(paths)
        stage_dir = staging_dir / f"{resolved_spawn_id}-{os.getpid()}-{secrets.token_hex(4)}"
        stage_dir.mkdir()
        staged_prompt = staged_starting_prompt(stage_dir, FIRST_LAUNCH)
        staged_prompt.parent.mkdir(parents=True)
        atomic_write_text(staged_prompt, prompt)
        stored_state = record_to_stored_state(record)
        atomic_write_text(
            stage_dir / "state.json",
            stored_state.model_dump_json(indent=2) + "\n",
        )
        source = HistorySource(kind="spawn", key=str(resolved_spawn_id))
        with lock_file(source.lock_path(runtime_root)):
            changes.mark(source)
            if launch_lock is not None:
                launch_lock.bind_staged(resolved_spawn_id, stage_dir)
            atomic_publish_dir(stage_dir, spawn_dir)
        return LaunchPaths.for_row(runtime_root, record)


def reopen_spawn(
    handle: LaunchHandle,
    runtime_root: Path,
    launch: LaunchInput,
    *,
    clock: Clock | None = None,
) -> LaunchPaths:
    """Start the next launch of a finished spawn under its launch lock; return its paths.

    ``handle`` is the proof that no runner of launch K is alive, so this is the only
    writer that may set ``launch`` on a v3 row. Order: K's row is copied to
    ``launch_archive`` (idempotent, so a crash here repeats it), ``launches/K+1/`` is
    created with the prompt, then one ``state.json`` write turns the row into launch
    K+1. K's outcome must already be written: the archive is terminal by construction.

    ``launch.parent_id`` must be the row's own: moving a spawn to another parent needs
    the ancestry check under ``spawns_flock`` that P5b's cycle transaction adds, and
    this function does not yet take that lock.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = str(handle.spawn_id)
    observed = _read_state(paths.spawns_dir, spawn_id, include_prompt=False)
    if observed is None:
        raise ValueError(f"spawn row not found: {spawn_id}")
    if observed.terminal is None:
        raise ValueError(f"Cannot reopen {spawn_id}: its last launch has no outcome")
    if launch.parent_id != observed.parent_id:
        raise ValueError(
            f"Cannot reopen {spawn_id} under a different parent: re-parenting needs "
            "the cycle-checked transaction that is not implemented yet"
        )
    k = observed.launch_number
    started = (clock or RealClock()).utc_now_iso()

    def next_launch(current: SpawnRecord) -> SpawnRecord:
        terminal = current.terminal
        if terminal is None or current.launch_number != k:
            raise ValueError(f"{spawn_id} changed under its launch lock")
        atomic_write_text(
            launch_archive(runtime_root, handle.spawn_id, k),
            record_to_stored_state(current).model_dump_json(indent=2) + "\n",
        )
        following = LaunchPaths.reserved(runtime_root, handle.spawn_id, k + 1)
        following.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(following.starting_prompt, launch.prompt)
        effective_goal = launch.goal if launch.goal is not None else current.goal
        owed = current.owed_launches
        if current.launch_mode == BACKGROUND_LAUNCH_MODE and current.parent_id:
            owed = (*owed, OwedLaunch(n=k, parent_id=current.parent_id))
        return current.model_copy(
            update={
                **_launch_fields(
                    runtime_root,
                    launch,
                    goal=effective_goal,
                    desc=current.desc,
                    forked_from_history_id=current.forked_from_history_id,
                    started_at=started,
                    status=SpawnStatus.RUNNING,
                ),
                "session_instance_id": None,
                "goal": effective_goal,
                "launch": LaunchRef(n=k + 1),
                "prior_usage": current.prior_usage.plus(terminal),
                "owed_launches": owed,
            }
        )

    result = _write_state_locked(
        paths.spawns_dir, spawn_id, next_launch, allow_terminal_overwrite=True
    )
    if not isinstance(result, Applied):
        raise ValueError(f"Cannot reopen {spawn_id}: {type(result).__name__}")
    return LaunchPaths.for_row(runtime_root, result.after)


def remove_spawn_events(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    held: LaunchHandle | None = None,
) -> None:
    """Remove per-spawn state for one spawn.

    This narrow rollback seam is for failed spawn preparation before any runner
    can observe or act on the row. It is not a user-facing delete/archive path.
    """

    delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda record: record is not None and _is_active_spawn_status(record.status),
        held=held,
    )


def update_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    chat_id: str | None = None,
    current_chat_id: str | None = None,
    run_boundary: RunBoundaryOutcome | None = None,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    runner_created_at_epoch: float | None = None,
    resident_rearm_count: int | None = None,
    harness_session_id: str | None = None,
    control_root: str | None = None,
    task_cwd: str | None = None,
    execution_cwd: str | None = None,
    claude_config_dir: str | None = None,
    desc: str | None = None,
    work_id: str | None = None,
    launch_policy_snapshot: LaunchPolicySnapshot | None = None,
) -> None:
    """Update v2 spawn metadata under the per-spawn state lock."""

    paths = RuntimePaths.from_root_dir(runtime_root)

    resolved_runner_created_at_epoch = runner_created_at_epoch
    if runner_pid is not None and resolved_runner_created_at_epoch is None:
        resolved_runner_created_at_epoch = _runner_created_at_epoch_for_pid(runner_pid)

    def merge(current: SpawnRecord) -> SpawnRecord:
        updates: dict[str, object] = {}
        if run_boundary is not None:
            updates["run_boundary"] = run_boundary
        if chat_id is not None:
            updates["chat_id"] = chat_id
        if current_chat_id is not None:
            updates["current_chat_id"] = current_chat_id.strip() or None
        if launch_mode is not None:
            updates["launch_mode"] = launch_mode
        if worker_pid is not None:
            updates["worker_pid"] = worker_pid
        if runner_pid is not None:
            updates["runner_pid"] = runner_pid
            updates["runner_created_at_epoch"] = resolved_runner_created_at_epoch
        elif runner_created_at_epoch is not None:
            updates["runner_created_at_epoch"] = runner_created_at_epoch
        if resident_rearm_count is not None:
            # Rearm grants are spawn-scoped, so a stale attempt must never lower
            # the authoritative count written by an earlier attempt.
            updates["resident_rearm_count"] = max(
                current.resident_rearm_count,
                resident_rearm_count,
            )
        if harness_session_id is not None:
            updates["harness_session_id"] = harness_session_id
        if control_root is not None:
            updates["control_root"] = normalize_path_for_write(control_root)
        if task_cwd is not None:
            updates["task_cwd"] = normalize_path_for_write(task_cwd)
        if execution_cwd is not None:
            updates["execution_cwd"] = normalize_path_for_write(execution_cwd)
        if claude_config_dir is not None:
            updates["claude_config_dir"] = claude_config_dir
        if desc is not None:
            updates["desc"] = desc
        if work_id is not None:
            updates["work_id"] = work_id.strip() or None
        if launch_policy_snapshot is not None:
            updates["launch_policy_snapshot"] = launch_policy_snapshot
        return current.model_copy(update=updates)

    outcome = _write_state_locked(
        paths.spawns_dir,
        str(spawn_id),
        merge,
        allow_terminal_overwrite=True,
    )
    if not isinstance(outcome, Applied):
        return
    from meridian.lib.core.telemetry import (
        LifecycleEvent,
        next_spawn_sequence,
        notify_observers,
    )

    record = outcome.after
    notify_observers(
        LifecycleEvent(
            event="spawn.updated",
            spawn_id=str(record.id),
            harness_id=record.harness or "",
            model=record.model or "",
            agent=record.agent,
            ts=datetime.now(tz=UTC),
            seq=next_spawn_sequence(record.id),
            payload={
                "launch_mode": launch_mode,
                "chat_id": chat_id,
                "worker_pid": worker_pid,
                "runner_pid": runner_pid,
                "runner_created_at_epoch": resolved_runner_created_at_epoch,
                "harness_session_id": harness_session_id,
                "control_root": control_root,
                "task_cwd": task_cwd,
                "execution_cwd": execution_cwd,
                "claude_config_dir": claude_config_dir,
                "desc": desc,
                "work_id": work_id,
            },
        )
    )


def finalize_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    status: TerminalSpawnStatus,
    exit_code: int,
    *,
    origin: SpawnOrigin,
    duration_secs: float | None = None,
    usage: TokenUsage | None = None,
    finished_at: str | None = None,
    error: str | None = None,
    managed_scopes_pending: bool = False,
    clock: Clock | None = None,
) -> LockedMutationResult:
    """Finalize under the lock when terminal authority accepts the transition."""
    resolved_clock = clock or RealClock()
    paths = RuntimePaths.from_root_dir(runtime_root)

    def finalize(current: SpawnRecord) -> SpawnRecord | Decline:
        if not _is_active_spawn_status(current.status):
            logger.info(
                "Finalize rejected: spawn already terminal.",
                spawn_id=str(spawn_id),
                current_status=current.status,
                current_terminal_origin=current.terminal.origin if current.terminal else None,
                attempted_status=status,
                attempted_origin=origin,
                attempted_error=error,
            )
            return Decline("spawn is already terminal")
        published_at = resolved_clock.utc_now_iso()
        return apply_finalize(
            current,
            status,
            exit_code,
            origin=origin,
            finished_at=finished_at or published_at,
            published_at=published_at,
            duration_secs=duration_secs,
            usage=usage,
            error=error,
            managed_scopes_pending=managed_scopes_pending,
        )

    return _write_state_locked(
        paths.spawns_dir,
        str(spawn_id),
        finalize,
        allow_terminal_overwrite=True,
    )


def record_spawn_launch_facts(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    runner_created_at_epoch: float | None = None,
) -> LockedMutationResult:
    """Record launch facts on an active row under the per-spawn lock."""

    paths = RuntimePaths.from_root_dir(runtime_root)

    resolved_runner_created_at_epoch = runner_created_at_epoch
    if runner_pid is not None and resolved_runner_created_at_epoch is None:
        resolved_runner_created_at_epoch = _runner_created_at_epoch_for_pid(runner_pid)

    def record_launch_facts(current: SpawnRecord) -> SpawnRecord | Decline:
        if not _is_active_spawn_status(current.status):
            return Decline("spawn is not active")
        return apply_launch_facts(
            current,
            launch_mode=launch_mode,
            worker_pid=worker_pid,
            runner_pid=runner_pid,
            runner_created_at_epoch=resolved_runner_created_at_epoch,
        )

    return _write_state_locked(paths.spawns_dir, str(spawn_id), record_launch_facts)


def _spawn_sort_key(spawn: SpawnRecord) -> tuple[int, str]:
    if is_spawn_id(spawn.id):
        return (int(spawn.id[1:]), spawn.id)
    return (10**9, spawn.id)


@dataclass(frozen=True)
class SpawnScan:
    """Immutable partition of valid rows and quarantined persisted siblings."""

    records: tuple[SpawnRecord, ...]
    quarantines: tuple[SpawnStateQuarantineReport, ...]

    @classmethod
    def ordered(
        cls,
        records: Sequence[SpawnRecord],
        quarantines: Sequence[SpawnStateQuarantineReport],
    ) -> SpawnScan:
        """Build a scan with the canonical persisted-spawn ordering."""
        return cls(tuple(sorted(records, key=_spawn_sort_key)), tuple(quarantines))


def quarantine_hint(spawn_ids: Sequence[str]) -> str:
    """Quarantined IDs behind the repair pointer, for commands that skip or refuse them."""
    return f"{len(spawn_ids)} spawns; run `meridian doctor`: {', '.join(spawn_ids)}"


def list_spawns(
    runtime_root: Path,
    *,
    parent_id: str | None = None,
    work_id: str | None = None,
) -> SpawnScan:
    """Partition valid persisted rows from structured quarantine reports."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    spawns: list[SpawnRecord] = []
    quarantines: list[SpawnStateQuarantineReport] = []
    for spawn_id in _scan_spawn_ids(paths.spawns_dir):
        try:
            record = _read_state(paths.spawns_dir, spawn_id, include_prompt=False)
        except SpawnStateQuarantined as exc:
            quarantines.append(exc.report)
            continue
        if record is not None:
            spawns.append(record)

    if parent_id is not None:
        spawns = [spawn for spawn in spawns if spawn.parent_id == parent_id]
    if work_id is not None:
        spawns = [spawn for spawn in spawns if spawn.work_id == work_id]

    return SpawnScan.ordered(spawns, quarantines)


def read_launch_paths(runtime_root: Path, spawn_id: SpawnId | str) -> LaunchPaths:
    """Read the published row once and return its launch's paths.

    For a process that adopts a launch it did not publish and holds only the ID (the
    background worker) or sits at an ID-only command boundary. The publisher hands out
    the launch it wrote; read surfaces use the row they already hold.
    """

    row = get_spawn(runtime_root, spawn_id)
    if row is None:
        raise ValueError(f"spawn row not found: {spawn_id}")
    return LaunchPaths.for_row(runtime_root, row)


def get_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
) -> SpawnRecord | None:
    """Return one spawn by ID.

    Reads ``spawns/<id>/state.json`` and its prompt body without locking.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    return _read_state(paths.spawns_dir, str(spawn_id))
