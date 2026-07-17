"""File-backed spawn state store for a Meridian runtime root.

Also includes file-backed ID generation for spawns and sessions.
"""

from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import psutil
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.core.clock import Clock, RealClock
from meridian.lib.core.domain import SpawnStatus, TokenUsage
from meridian.lib.core.launch_policy_snapshot import (
    LaunchPolicySnapshot as LaunchPolicySnapshot,
)
from meridian.lib.core.spawn_lifecycle import (
    ACTIVE_SPAWN_STATUSES as _ACTIVE_SPAWN_STATUSES,
)
from meridian.lib.core.spawn_lifecycle import (
    is_active_spawn_status as _is_active_spawn_status,
)
from meridian.lib.core.spawn_lifecycle import (
    is_terminal_spawn_status as _is_terminal_spawn_status,
)
from meridian.lib.core.spawn_start import SpawnStartMetadata, derive_display_label
from meridian.lib.core.types import SpawnId
from meridian.lib.state.atomic import atomic_publish_dir, atomic_write_text
from meridian.lib.state.event_store import lock_file
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.model import (
    CancelIntent as CancelIntent,
)
from meridian.lib.state.spawn.model import (
    LaunchMode as LaunchMode,
)
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
    is_safe_spawn_dir_name as _is_safe_spawn_dir_name,
)
from meridian.lib.state.spawn.repository import read_state as _read_state
from meridian.lib.state.spawn.repository import record_to_stored_state
from meridian.lib.state.spawn.repository import scan_spawn_ids as _scan_spawn_ids
from meridian.lib.state.spawn.repository import write_state_locked as _write_state_locked
from meridian.lib.state.spawn.terminal_policy import decide_terminal_write
from meridian.lib.state.spawn.transitions import (
    apply_cancel_intent,
    apply_finalize,
    apply_mark_finalizing,
    apply_mark_running,
    apply_record_exited,
    apply_runner_exit,
)
from meridian.lib.state.spawn_aggregate import delete_published_spawn

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# ID generation (absorbed from state/id_gen.py)
# ---------------------------------------------------------------------------


def _spawn_id_index(spawn_id: SpawnId | str) -> int:
    value = str(spawn_id)
    if is_spawn_id_shape(value):
        return int(value[1:])
    return 0


def is_spawn_id_shape(spawn_id: SpawnId | str) -> bool:
    """Return whether *spawn_id* has Meridian's persisted spawn ID shape."""

    value = str(spawn_id)
    suffix = value[1:]
    return len(value) > 1 and value[0] == "p" and suffix.isascii() and suffix.isdigit()


def _spawn_counter_path(paths: RuntimePaths) -> Path:
    return paths.root_dir / "spawn-id-counter"


def _read_spawn_counter(paths: RuntimePaths) -> int:
    counter_path = _spawn_counter_path(paths)
    if not counter_path.is_file():
        return 0
    try:
        return int(counter_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _seed_spawn_counter_from_dirs(paths: RuntimePaths) -> int:
    return max(
        (_spawn_id_index(spawn_id) for spawn_id in _scan_spawn_ids(paths.spawns_dir)),
        default=0,
    )


def reserve_spawn_id(
    runtime_root: Path,
) -> SpawnId:
    """Reserve and return the next spawn ID (`p1`, `p2`, ...).

    Reservation is persisted separately from spawn rows so callers can compose
    launch context with the final ID before writing lifecycle-visible events.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    with lock_file(paths.spawns_flock):
        current = _read_spawn_counter(paths)
        if current == 0 and not _spawn_counter_path(paths).is_file():
            current = _seed_spawn_counter_from_dirs(paths)
        next_value = current + 1
        atomic_write_text(_spawn_counter_path(paths), f"{next_value}\n")
        return SpawnId(f"p{next_value}")


def next_spawn_id(
    runtime_root: Path,
) -> SpawnId:
    """Return the next spawn ID (`p1`, `p2`, ...) for a state root."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    with lock_file(paths.spawns_flock):
        seed = _seed_spawn_counter_from_dirs(paths)
        next_from_existing = SpawnId(f"p{seed + 1}")
        next_from_counter = SpawnId(f"p{_read_spawn_counter(paths) + 1}")
        return max((next_from_existing, next_from_counter), key=_spawn_id_index)


ACTIVE_SPAWN_STATUSES = _ACTIVE_SPAWN_STATUSES
is_active_spawn_status = _is_active_spawn_status


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
        desc=metadata.desc if metadata.desc is not None else desc,
        work_id=metadata.work_id if metadata.work_id is not None else work_id,
        goal=metadata.goal if metadata.goal is not None else goal,
        launch_policy_snapshot=(
            metadata.launch_policy_snapshot
            if metadata.launch_policy_snapshot is not None
            else launch_policy_snapshot
        ),
    )


class FinalizeOutcome(BaseModel):
    """Result of a finalize write with the exact post-write projection.

    ``transitioned`` preserves the "this writer moved an active row to a
    terminal state" signal. ``wrote`` distinguishes rejected reconciler attempts
    from accepted authoritative metadata/override writes. ``snapshot`` is the
    store projection immediately after the state write, computed under the same
    lock, so callers can emit events without a post-write reread race.
    """

    model_config = ConfigDict(frozen=True)

    transitioned: bool
    wrote: bool
    snapshot: SpawnRecord | None


def _ensure_staging_dir(paths: RuntimePaths) -> Path:
    staging_dir = paths.spawns_dir / ".staging"
    if os.path.lexists(staging_dir) and (
        staging_dir.is_symlink() or not staging_dir.is_dir()
    ):
        raise NotADirectoryError(
            f"Spawn staging container must be a real directory: {staging_dir}"
        )
    staging_dir.mkdir(parents=True, exist_ok=True)
    if staging_dir.is_symlink() or not staging_dir.is_dir():
        raise NotADirectoryError(
            f"Spawn staging container must be a real directory: {staging_dir}"
        )
    return staging_dir


def gc_abandoned_stages(runtime_root: Path) -> None:
    """Best-effort cleanup of incomplete spawn publication stages."""

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
        except OSError:
            return
        for entry in entries:
            with suppress(OSError):
                if not entry.is_symlink() and entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()


def start_spawn(
    runtime_root: Path,
    *,
    chat_id: str,
    owner_chat_id: str | None = None,
    parent_id: str | None = None,
    model: str,
    agent: str,
    agent_path: str | None = None,
    skills: tuple[str, ...] = (),
    skill_paths: tuple[str, ...] = (),
    harness: str,
    kind: str = "child",
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
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    runner_created_at_epoch: float | None = None,
    launch_policy_snapshot: LaunchPolicySnapshot | None = None,
    status: SpawnStatus = "running",
    started_at: str | None = None,
    clock: Clock | None = None,
) -> SpawnId:
    """Create a v2 spawn state directory and return the spawn ID."""

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
    normalized_goal: str | None = None
    if start_metadata.goal is not None:
        normalized_goal = start_metadata.goal.strip()
        if not normalized_goal:
            raise ValueError("--goal cannot be empty")

    resolved_runner_created_at_epoch = runner_created_at_epoch
    if resolved_runner_created_at_epoch is None:
        resolved_runner_created_at_epoch = _runner_created_at_epoch_for_pid(runner_pid)

    resolved_launch_policy_snapshot = (
        launch_policy_snapshot or start_metadata.launch_policy_snapshot
    )

    with lock_file(paths.spawns_flock):
        if spawn_id is not None:
            explicit_spawn_id = str(spawn_id)
            if not _is_safe_spawn_dir_name(explicit_spawn_id):
                raise ValueError(f"Invalid spawn ID: {explicit_spawn_id}")
            resolved_spawn_id = SpawnId(explicit_spawn_id)
        else:
            current = _read_spawn_counter(paths)
            if current == 0 and not _spawn_counter_path(paths).is_file():
                current = _seed_spawn_counter_from_dirs(paths)
            next_value = current + 1
            atomic_write_text(_spawn_counter_path(paths), f"{next_value}\n")
            resolved_spawn_id = SpawnId(f"p{next_value}")
        record = SpawnRecord(
            id=str(resolved_spawn_id),
            chat_id=chat_id,
            owner_chat_id=owner_chat_id,
            parent_id=parent_id,
            originating_bash_id=os.environ.get("MERIDIAN_PI_BASH_ID") or None,
            model=model,
            agent=agent,
            agent_path=agent_path,
            skills=skills,
            skill_paths=skill_paths,
            harness=harness,
            kind=kind,
            desc=start_metadata.desc,
            work_id=(
                start_metadata.work_id.strip() or None
                if start_metadata.work_id is not None
                else None
            ),
            goal=normalized_goal,
            display_label=derive_display_label(
                goal=normalized_goal,
                desc=start_metadata.desc,
                prompt=prompt,
            ),
            harness_session_id=harness_session_id,
            control_root=control_root,
            task_cwd=task_cwd,
            execution_cwd=execution_cwd,
            claude_config_dir=None,
            launch_mode=launch_mode,
            worker_pid=worker_pid,
            runner_pid=runner_pid,
            runner_created_at_epoch=resolved_runner_created_at_epoch,
            status=status,
            prompt=prompt,
            started_at=started,
            last_attempt_exited_at=None,
            last_attempt_exit_code=None,
            runner_exit_code=None,
            runner_exit_status=None,
            runner_exit_error=None,
            runner_exit_at=None,
            cancel_intent=None,
            finished_at=None,
            exit_code=None,
            duration_secs=None,
            total_cost_usd=None,
            input_tokens=None,
            output_tokens=None,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
            reasoning_tokens=None,
            cost_is_estimate=False,
            error=None,
            terminal_origin=None,
            launch_policy_snapshot=resolved_launch_policy_snapshot,
        )
        spawn_dir = paths.spawns_dir / str(resolved_spawn_id)
        staging_dir = _ensure_staging_dir(paths)
        stage_dir = staging_dir / f"{resolved_spawn_id}-{os.getpid()}-{secrets.token_hex(4)}"
        stage_dir.mkdir()
        atomic_write_text(stage_dir / "starting-prompt.md", prompt)
        stored_state = record_to_stored_state(record)
        atomic_write_text(
            stage_dir / "state.json",
            stored_state.model_dump_json(indent=2) + "\n",
        )
        atomic_publish_dir(stage_dir, spawn_dir)
        return resolved_spawn_id


def remove_spawn_events(
    runtime_root: Path,
    spawn_id: SpawnId | str,
) -> None:
    """Remove v2 state for one spawn.

    This narrow rollback seam is for failed spawn preparation before any runner
    can observe or act on the row. It is not a user-facing delete/archive path.
    """

    delete_published_spawn(
        runtime_root,
        spawn_id,
        can_delete=lambda record: record is not None and record.status == "queued",
    )


def update_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    chat_id: str | None = None,
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
    error: str | None = None,
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
        if chat_id is not None:
            updates["chat_id"] = chat_id
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
            updates["control_root"] = control_root
        if task_cwd is not None:
            updates["task_cwd"] = task_cwd
        if execution_cwd is not None:
            updates["execution_cwd"] = execution_cwd
        if claude_config_dir is not None:
            updates["claude_config_dir"] = claude_config_dir
        if error is not None:
            updates["error"] = error
        if desc is not None:
            updates["desc"] = desc
        if work_id is not None:
            updates["work_id"] = work_id.strip() or None
        if launch_policy_snapshot is not None:
            updates["launch_policy_snapshot"] = launch_policy_snapshot
        return current.model_copy(update=updates)

    try:
        record = _write_state_locked(
            paths.spawns_dir,
            str(spawn_id),
            merge,
            allow_terminal_overwrite=True,
        )
    except FileNotFoundError:
        return
    from meridian.lib.core.telemetry import (
        LifecycleEvent,
        next_spawn_sequence,
        notify_observers,
    )

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
                "error": error,
                "desc": desc,
                "work_id": work_id,
            },
        )
    )


def record_spawn_exited(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    exit_code: int,
    exited_at: str | None = None,
    clock: Clock | None = None,
) -> SpawnRecord | None:
    """Record latest drained harness-attempt exit metadata."""

    resolved_clock = clock or RealClock()
    paths = RuntimePaths.from_root_dir(runtime_root)

    def merge_exit(current: SpawnRecord) -> SpawnRecord:
        return apply_record_exited(
            current,
            exit_code=exit_code,
            exited_at=exited_at or resolved_clock.utc_now_iso(),
        )

    try:
        return _write_state_locked(
            paths.spawns_dir,
            str(spawn_id),
            merge_exit,
            allow_terminal_overwrite=True,
        )
    except FileNotFoundError:
        return None


def record_runner_exit(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    status: TerminalSpawnStatus,
    exit_code: int,
    error: str | None = None,
    exited_at: str | None = None,
    clock: Clock | None = None,
) -> SpawnRecord | None:
    """Record runner-resolved terminal intent before finalization."""

    if not _is_terminal_spawn_status(status):
        raise ValueError(f"runner exit status must be terminal, got {status!r}")

    resolved_clock = clock or RealClock()
    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_exited_at = exited_at or resolved_clock.utc_now_iso()

    class _RunnerExitSkipped(Exception):
        def __init__(self, snapshot: SpawnRecord | None) -> None:
            self.snapshot = snapshot

    def merge_exit(current: SpawnRecord) -> SpawnRecord:
        if not is_active_spawn_status(current.status):
            raise _RunnerExitSkipped(current)
        return apply_runner_exit(
            current,
            status=status,
            exit_code=exit_code,
            error=error,
            exited_at=resolved_exited_at,
        )

    if _read_state(paths.spawns_dir, str(spawn_id)) is None:
        return None
    try:
        return _write_state_locked(
            paths.spawns_dir,
            str(spawn_id),
            merge_exit,
        )
    except FileNotFoundError:
        return None
    except _RunnerExitSkipped:
        return None


def record_cancel_intent(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    exit_code: int,
    error: str | None,
    requested_by: str = "user",
    requested_at: str | None = None,
    clock: Clock | None = None,
) -> SpawnRecord | None:
    """Record a durable spawn-level cancellation request under the spawn lock."""

    if requested_by not in {"user", "system"}:
        raise ValueError(f"cancel requested_by must be 'user' or 'system', got {requested_by!r}")

    resolved_clock = clock or RealClock()
    intent = CancelIntent(
        requested_at=requested_at or resolved_clock.utc_now_iso(),
        exit_code=exit_code,
        error=error,
        requested_by=cast("Literal['user', 'system']", requested_by),
    )
    paths = RuntimePaths.from_root_dir(runtime_root)

    class _CancelIntentSkipped(Exception):
        def __init__(self, snapshot: SpawnRecord) -> None:
            self.snapshot = snapshot

    def merge_intent(current: SpawnRecord) -> SpawnRecord:
        if not is_active_spawn_status(current.status):
            raise _CancelIntentSkipped(current)
        return apply_cancel_intent(current, intent=intent)

    if _read_state(paths.spawns_dir, str(spawn_id)) is None:
        return None
    try:
        return _write_state_locked(paths.spawns_dir, str(spawn_id), merge_intent)
    except FileNotFoundError:
        return None
    except _CancelIntentSkipped as exc:
        return exc.snapshot


def finalize_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    status: SpawnStatus,
    exit_code: int,
    *,
    origin: SpawnOrigin,
    duration_secs: float | None = None,
    usage: TokenUsage | None = None,
    finished_at: str | None = None,
    error: str | None = None,
    clock: Clock | None = None,
) -> FinalizeOutcome:
    """Finalize a spawn state and return write/transition details.

    Writes only when the terminal write policy accepts the incoming finalize.
    ``outcome.transitioned`` is True when the spawn was active (queued,
    running, or finalizing) before this call, meaning this writer is the one
    that moved it to a terminal state. It is False when the spawn was already
    terminal or does not exist. Authoritative finalize events may still write
    and replace a reconciler terminal tuple; callers should use
    ``outcome.wrote`` and ``outcome.snapshot`` for post-write event emission.
    """
    resolved_clock = clock or RealClock()
    paths = RuntimePaths.from_root_dir(runtime_root)

    class _FinalizeRejected(Exception):
        def __init__(self, snapshot: SpawnRecord) -> None:
            self.snapshot = snapshot

    was_active = False

    def finalize(current: SpawnRecord) -> SpawnRecord:
        nonlocal was_active
        decision = decide_terminal_write(
            current_status=current.status,
            current_terminal_origin=current.terminal_origin,
            incoming_origin=origin,
        )
        if decision.disposition == "reject":
            logger.info(
                "Finalize rejected by terminal write policy.",
                spawn_id=str(spawn_id),
                current_status=current.status,
                current_terminal_origin=current.terminal_origin,
                attempted_status=status,
                attempted_origin=origin,
                attempted_error=error,
            )
            raise _FinalizeRejected(current)
        was_active = is_active_spawn_status(current.status)
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
        )

    record = _read_state(paths.spawns_dir, str(spawn_id))
    if record is None:
        decide_terminal_write(
            current_status=None,
            current_terminal_origin=None,
            incoming_origin=origin,
        )
        return FinalizeOutcome(transitioned=False, wrote=False, snapshot=None)
    try:
        committed = _write_state_locked(
            paths.spawns_dir,
            str(spawn_id),
            finalize,
            allow_terminal_overwrite=True,
        )
    except _FinalizeRejected as exc:
        return FinalizeOutcome(transitioned=False, wrote=False, snapshot=exc.snapshot)
    return FinalizeOutcome(
        transitioned=was_active,
        wrote=True,
        snapshot=committed,
    )


def mark_finalizing(
    runtime_root: Path,
    spawn_id: SpawnId | str,
) -> bool:
    """CAS transition `running -> finalizing` under the per-spawn lock."""

    transitioned, _snapshot = mark_finalizing_with_snapshot(
        runtime_root,
        spawn_id,
    )
    return transitioned


def mark_finalizing_with_snapshot(
    runtime_root: Path,
    spawn_id: SpawnId | str,
) -> tuple[bool, SpawnRecord | None]:
    """CAS transition running -> finalizing and return the post-write projection."""

    paths = RuntimePaths.from_root_dir(runtime_root)

    class _NoTransition(Exception):
        def __init__(self, snapshot: SpawnRecord | None) -> None:
            self.snapshot = snapshot

    def transition(current: SpawnRecord) -> SpawnRecord:
        if current.status != "running":
            raise _NoTransition(current)
        return apply_mark_finalizing(current)

    if _read_state(paths.spawns_dir, str(spawn_id)) is None:
        return False, None
    try:
        committed = _write_state_locked(paths.spawns_dir, str(spawn_id), transition)
    except _NoTransition as exc:
        return False, exc.snapshot
    return True, committed


def mark_spawn_running(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    runner_created_at_epoch: float | None = None,
) -> bool:
    changed, _snapshot = mark_spawn_running_with_snapshot(
        runtime_root,
        spawn_id,
        launch_mode=launch_mode,
        worker_pid=worker_pid,
        runner_pid=runner_pid,
        runner_created_at_epoch=runner_created_at_epoch,
    )
    return changed


def mark_spawn_running_with_snapshot(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    runner_created_at_epoch: float | None = None,
) -> tuple[bool, SpawnRecord | None]:
    """Mark a spawn running and return the post-write projection without rereading."""

    paths = RuntimePaths.from_root_dir(runtime_root)

    changed = False
    resolved_runner_created_at_epoch = runner_created_at_epoch
    if runner_pid is not None and resolved_runner_created_at_epoch is None:
        resolved_runner_created_at_epoch = _runner_created_at_epoch_for_pid(runner_pid)

    class _NoTransition(Exception):
        def __init__(self, snapshot: SpawnRecord) -> None:
            self.snapshot = snapshot

    def merge(current: SpawnRecord) -> SpawnRecord:
        nonlocal changed
        if not is_active_spawn_status(current.status):
            raise _NoTransition(current)
        changed = current.status != "running"
        return apply_mark_running(
            current,
            launch_mode=launch_mode,
            worker_pid=worker_pid,
            runner_pid=runner_pid,
            runner_created_at_epoch=resolved_runner_created_at_epoch,
        )

    try:
        committed = _write_state_locked(paths.spawns_dir, str(spawn_id), merge)
    except FileNotFoundError:
        return False, None
    except _NoTransition as exc:
        return False, exc.snapshot
    return changed, committed


def _spawn_sort_key(spawn: SpawnRecord) -> tuple[int, str]:
    if len(spawn.id) >= 2 and spawn.id[0] in {"p", "r"} and spawn.id[1:].isdigit():
        return (int(spawn.id[1:]), spawn.id)
    return (10**9, spawn.id)


def _apply_spawn_filters(
    spawns: list[SpawnRecord],
    filters: Mapping[str, Any],
) -> list[SpawnRecord]:
    filtered: list[SpawnRecord] = []
    for spawn in spawns:
        spawn_data = spawn.model_dump()
        keep = True
        for key, expected in filters.items():
            if expected is None:
                continue
            if key == "owner_chat_id":
                from meridian.lib.state.session_identity import spawn_owner_chat_id

                if spawn_owner_chat_id(spawn) != expected:
                    keep = False
                    break
                continue
            if key not in spawn_data:
                continue
            if spawn_data[key] != expected:
                keep = False
                break
        if keep:
            filtered.append(spawn)
    return filtered


def list_spawns(
    runtime_root: Path,
    filters: Mapping[str, Any] | None = None,
) -> list[SpawnRecord]:
    """List v2 spawn records with optional equality filters."""

    paths = RuntimePaths.from_root_dir(runtime_root)
    spawns: list[SpawnRecord] = []
    for spawn_id in _scan_spawn_ids(paths.spawns_dir):
        try:
            record = _read_state(paths.spawns_dir, spawn_id, include_prompt=False)
        except ValidationError:
            continue
        if record is not None:
            spawns.append(record)

    if filters:
        spawns = _apply_spawn_filters(spawns, filters)

    return sorted(spawns, key=_spawn_sort_key)


def get_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
) -> SpawnRecord | None:
    """Return one spawn by ID.

    Reads ``spawns/<id>/state.json`` and its prompt body without locking.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    return _read_state(paths.spawns_dir, str(spawn_id))


def spawn_stats(
    runtime_root: Path,
) -> dict[str, Any]:
    """Aggregate high-level spawn stats from v2 state records."""

    spawns = list_spawns(runtime_root)
    by_status: dict[str, int] = {}
    by_model: dict[str, int] = {}
    total_duration_secs = 0.0
    total_cost_usd = 0.0
    total_input_tokens = 0
    total_output_tokens = 0

    for spawn in spawns:
        by_status[spawn.status] = by_status.get(spawn.status, 0) + 1
        if spawn.model is not None:
            by_model[spawn.model] = by_model.get(spawn.model, 0) + 1
        if spawn.duration_secs is not None:
            total_duration_secs += spawn.duration_secs
        if spawn.total_cost_usd is not None:
            total_cost_usd += spawn.total_cost_usd
        if spawn.input_tokens is not None:
            total_input_tokens += spawn.input_tokens
        if spawn.output_tokens is not None:
            total_output_tokens += spawn.output_tokens

    return {
        "total_runs": len(spawns),
        "by_status": by_status,
        "by_model": by_model,
        "total_duration_secs": total_duration_secs,
        "total_cost_usd": total_cost_usd,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
    }
