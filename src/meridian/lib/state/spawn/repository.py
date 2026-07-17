"""Spawn v2 state-file persistence helpers.

The helpers in this module persist one ``state.json`` per spawn under the
runtime spawns directory.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES
from meridian.lib.core.types import SpawnId
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.model import CancelIntent, SpawnRecord

if TYPE_CHECKING:
    from meridian.lib.core.domain import SpawnStatus
    from meridian.lib.state.spawn.model import LaunchMode, SpawnOrigin, TerminalSpawnStatus


class StoredSpawnState(BaseModel):
    """On-disk v2 ``state.json`` representation.

    The prompt body is stored separately in ``starting-prompt.md``; this model
    keeps only ``prompt_length`` metadata so state reads can stay lightweight.
    """

    model_config = ConfigDict(frozen=True)

    v: Literal[2]
    id: str
    chat_id: str | None = None
    owner_chat_id: str | None = None
    parent_id: str | None = None
    originating_bash_id: str | None = None
    model: str | None = None
    agent: str | None = None
    agent_path: str | None = None
    skills: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    harness: str | None = None
    kind: str = "child"
    desc: str | None = None
    work_id: str | None = None
    goal: str | None = None
    display_label: str | None = None
    harness_session_id: str | None = None
    control_root: str | None = None
    task_cwd: str | None = None
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    launch_mode: str | None = None
    worker_pid: int | None = None
    runner_pid: int | None = None
    runner_created_at_epoch: float | None = None
    status: str = "unknown"
    started_at: str | None = None
    last_attempt_exited_at: str | None = None
    last_attempt_exit_code: int | None = None
    runner_exit_code: int | None = None
    runner_exit_status: str | None = None
    runner_exit_error: str | None = None
    runner_exit_at: str | None = None
    cancel_intent: CancelIntent | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    duration_secs: float | None = None
    total_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_is_estimate: bool = False
    error: str | None = None
    terminal_origin: str | None = None
    prompt_length: int | None = None
    launch_policy_snapshot: LaunchPolicySnapshot | None = None


def _spawn_dir(spawns_dir: Path, spawn_id: str) -> Path:
    return spawns_dir / spawn_id


def _state_path(spawns_dir: Path, spawn_id: str) -> Path:
    return _spawn_dir(spawns_dir, spawn_id) / "state.json"


def _prompt_path(spawns_dir: Path, spawn_id: str) -> Path:
    return _spawn_dir(spawns_dir, spawn_id) / "starting-prompt.md"


def spawn_lock_path(spawns_dir: Path, spawn_id: str) -> Path:
    """Return the stable external-writer lock for one published spawn."""
    return spawns_dir.parent / "locks" / "spawns" / f"{spawn_id}.lock"


def record_to_stored_state(
    record: SpawnRecord,
) -> StoredSpawnState:
    """Convert a spawn projection to v2 on-disk state without prompt body."""

    return StoredSpawnState(
        v=2,
        id=record.id,
        chat_id=record.chat_id,
        owner_chat_id=record.owner_chat_id,
        parent_id=record.parent_id,
        originating_bash_id=record.originating_bash_id,
        model=record.model,
        agent=record.agent,
        agent_path=record.agent_path,
        skills=record.skills,
        skill_paths=record.skill_paths,
        harness=record.harness,
        kind=record.kind,
        desc=record.desc,
        work_id=record.work_id,
        goal=record.goal,
        display_label=record.display_label,
        harness_session_id=record.harness_session_id,
        control_root=record.control_root,
        task_cwd=record.task_cwd,
        execution_cwd=record.execution_cwd,
        claude_config_dir=record.claude_config_dir,
        launch_mode=record.launch_mode,
        worker_pid=record.worker_pid,
        runner_pid=record.runner_pid,
        runner_created_at_epoch=record.runner_created_at_epoch,
        status=record.status,
        started_at=record.started_at,
        last_attempt_exited_at=record.last_attempt_exited_at,
        last_attempt_exit_code=record.last_attempt_exit_code,
        runner_exit_code=record.runner_exit_code,
        runner_exit_status=record.runner_exit_status,
        runner_exit_error=record.runner_exit_error,
        runner_exit_at=record.runner_exit_at,
        cancel_intent=record.cancel_intent,
        finished_at=record.finished_at,
        exit_code=record.exit_code,
        duration_secs=record.duration_secs,
        total_cost_usd=record.total_cost_usd,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cache_read_input_tokens=record.cache_read_input_tokens,
        cache_creation_input_tokens=record.cache_creation_input_tokens,
        reasoning_tokens=record.reasoning_tokens,
        cost_is_estimate=record.cost_is_estimate,
        error=record.error,
        terminal_origin=record.terminal_origin,
        prompt_length=len(record.prompt) if record.prompt is not None else None,
        launch_policy_snapshot=record.launch_policy_snapshot,
    )


def stored_state_to_record(
    stored: StoredSpawnState,
    prompt: str | None = None,
) -> SpawnRecord:
    """Convert v2 on-disk state to a ``SpawnRecord`` projection."""

    return SpawnRecord(
        id=stored.id,
        chat_id=stored.chat_id,
        owner_chat_id=stored.owner_chat_id,
        parent_id=stored.parent_id,
        originating_bash_id=stored.originating_bash_id,
        model=stored.model,
        agent=stored.agent,
        agent_path=stored.agent_path,
        skills=stored.skills,
        skill_paths=stored.skill_paths,
        harness=stored.harness,
        kind=stored.kind,
        desc=stored.desc,
        work_id=stored.work_id,
        goal=stored.goal,
        display_label=stored.display_label,
        harness_session_id=stored.harness_session_id,
        control_root=stored.control_root,
        task_cwd=stored.task_cwd,
        execution_cwd=stored.execution_cwd,
        claude_config_dir=stored.claude_config_dir,
        launch_mode=cast("LaunchMode | None", stored.launch_mode),
        worker_pid=stored.worker_pid,
        runner_pid=stored.runner_pid,
        runner_created_at_epoch=stored.runner_created_at_epoch,
        status=cast('SpawnStatus | Literal["unknown"]', stored.status),
        prompt=prompt,
        started_at=stored.started_at,
        last_attempt_exited_at=stored.last_attempt_exited_at,
        last_attempt_exit_code=stored.last_attempt_exit_code,
        runner_exit_code=stored.runner_exit_code,
        runner_exit_status=cast('TerminalSpawnStatus | None', stored.runner_exit_status),
        runner_exit_error=stored.runner_exit_error,
        runner_exit_at=stored.runner_exit_at,
        cancel_intent=stored.cancel_intent,
        finished_at=stored.finished_at,
        exit_code=stored.exit_code,
        duration_secs=stored.duration_secs,
        total_cost_usd=stored.total_cost_usd,
        input_tokens=stored.input_tokens,
        output_tokens=stored.output_tokens,
        cache_read_input_tokens=stored.cache_read_input_tokens,
        cache_creation_input_tokens=stored.cache_creation_input_tokens,
        reasoning_tokens=stored.reasoning_tokens,
        cost_is_estimate=stored.cost_is_estimate,
        error=stored.error,
        terminal_origin=cast("SpawnOrigin | None", stored.terminal_origin),
        launch_policy_snapshot=stored.launch_policy_snapshot,
    )


def read_prompt(spawns_dir: Path, spawn_id: str) -> str | None:
    """Read a spawn's starting prompt body, if present."""

    path = _prompt_path(spawns_dir, spawn_id)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _read_stored_state(spawns_dir: Path, spawn_id: str) -> StoredSpawnState | None:
    path = _state_path(spawns_dir, spawn_id)
    try:
        return StoredSpawnState.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def read_state(
    spawns_dir: Path,
    spawn_id: str,
    *,
    include_prompt: bool = True,
) -> SpawnRecord | None:
    """Read ``spawns/<id>/state.json`` and reconstruct a spawn record.

    List and filter paths should pass ``include_prompt=False`` so large
    ``starting-prompt.md`` bodies are not read unless a caller needs them.
    """

    stored = _read_stored_state(spawns_dir, spawn_id)
    if stored is None:
        return None
    prompt = read_prompt(spawns_dir, spawn_id) if include_prompt else None
    return stored_state_to_record(stored, prompt=prompt)


def _write_state(spawns_dir: Path, record: SpawnRecord) -> None:
    """Persist a record whose current-state transition was decided by the caller."""

    stored = record_to_stored_state(record)
    atomic_write_text(
        _state_path(spawns_dir, record.id),
        stored.model_dump_json(indent=2) + "\n",
    )


def write_state_locked(
    spawns_dir: Path,
    spawn_id: str,
    mutator: Callable[[SpawnRecord], SpawnRecord],
    *,
    allow_terminal_overwrite: bool = False,
) -> SpawnRecord:
    """Re-read, mutate, and persist one spawn under its stable per-spawn lock.

    Lock reentrancy is forbidden because a nested mutation could commit from a
    second snapshot and then be clobbered by the outer mutation's stale result.
    """

    with lock_file(spawn_lock_path(spawns_dir, spawn_id), reentrant=False):
        current = read_state(spawns_dir, spawn_id)
        if current is None:
            raise FileNotFoundError(_state_path(spawns_dir, spawn_id))
        updated = mutator(current)
        if updated.id != spawn_id:
            raise ValueError("Locked state mutator must not change spawn id")
        if current.status in TERMINAL_SPAWN_STATUSES and not allow_terminal_overwrite:
            raise ValueError(f"Refusing to overwrite terminal spawn state: {spawn_id}")
        _write_state(spawns_dir, updated)
        return updated


type SpawnDeletionPrecondition = Callable[[SpawnRecord | None], bool]


def is_safe_spawn_dir_name(name: str) -> bool:
    separators = {"/", "\\", os.sep}
    if os.altsep is not None:
        separators.add(os.altsep)
    return bool(name) and not name.startswith(".") and not any(
        separator in name for separator in separators
    )


def _restore_spawn_artifact_permissions(
    func: Callable[[str], object],
    path: str,
    exc_info: BaseException,
) -> None:
    if isinstance(exc_info, FileNotFoundError):
        return
    with suppress(OSError):
        os.chmod(path, stat.S_IWRITE)
    try:
        func(path)
    except OSError as error:
        raise exc_info from error


def delete_published_spawn(
    runtime_root: Path,
    spawn_id: SpawnId | str,
    *,
    can_delete: SpawnDeletionPrecondition,
) -> bool:
    """Delete one published spawn when its locked projection permits it.

    Every published-row deletion routes through this seam. A cleanup claim
    prevents deletion because it is durable at-least-once intent: the reaper
    must finish or clear the claim before artifact retention may remove it.
    Callers that also need ``spawns_flock`` must acquire it first.
    """

    paths = RuntimePaths.from_root_dir(runtime_root)
    resolved_spawn_id = str(spawn_id)
    if not is_safe_spawn_dir_name(resolved_spawn_id):
        raise ValueError(f"Invalid spawn ID: {resolved_spawn_id}")
    spawn_dir = paths.spawns_dir / resolved_spawn_id

    with lock_file(spawn_lock_path(paths.spawns_dir, resolved_spawn_id)):
        from meridian.lib.state.process_scope_projection import scope_projection_lock_path

        # Global order: spawn state, then process-scope projection.
        with lock_file(scope_projection_lock_path(runtime_root, resolved_spawn_id)):
            claim_path = spawn_dir / "reaper_cleanup_claim.json"
            if claim_path.exists() or not can_delete(
                read_state(paths.spawns_dir, resolved_spawn_id, include_prompt=False)
            ):
                return False
            if not spawn_dir.exists():
                return False
            try:
                shutil.rmtree(spawn_dir, onexc=_restore_spawn_artifact_permissions)
            except OSError:
                return False
            return True


def scan_spawn_ids(spawns_dir: Path) -> list[str]:
    """Return child directory names that contain a v2 ``state.json`` file."""

    try:
        entries = os.scandir(spawns_dir)
    except FileNotFoundError:
        return []

    with entries:
        return sorted(
            entry.name
            for entry in entries
            if entry.is_dir() and _state_path(spawns_dir, entry.name).is_file()
        )


__all__ = [
    "SpawnDeletionPrecondition",
    "StoredSpawnState",
    "delete_published_spawn",
    "is_safe_spawn_dir_name",
    "read_prompt",
    "read_state",
    "record_to_stored_state",
    "scan_spawn_ids",
    "spawn_lock_path",
    "stored_state_to_record",
    "write_state_locked",
]
