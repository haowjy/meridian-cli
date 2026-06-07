"""Spawn v2 state-file persistence helpers.

The helpers in this module persist one ``state.json`` per spawn under the
runtime spawns directory.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
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

    v: Literal[2] = 2
    revision: int

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


def _lock_path(spawns_dir: Path, spawn_id: str) -> Path:
    return _spawn_dir(spawns_dir, spawn_id) / "state.lock"


def record_to_stored_state(
    record: SpawnRecord,
    *,
    revision: int = 0,
) -> StoredSpawnState:
    """Convert a spawn projection to v2 on-disk state without prompt body."""

    return StoredSpawnState(
        revision=revision,
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


def read_state(spawns_dir: Path, spawn_id: str) -> SpawnRecord | None:
    """Read ``spawns/<id>/state.json`` and reconstruct a spawn record."""

    stored = _read_stored_state(spawns_dir, spawn_id)
    if stored is None:
        return None
    return stored_state_to_record(stored, prompt=read_prompt(spawns_dir, spawn_id))


def write_state(
    spawns_dir: Path,
    record: SpawnRecord,
    *,
    revision: int | None = None,
    allow_terminal_overwrite: bool = False,
) -> int:
    """Write one spawn's v2 state file and return the committed revision.

    This owner hot path deliberately does not acquire locks. It performs a
    best-effort terminal monotonicity guard by rereading on-disk state before
    writing and refusing to overwrite any already-terminal record.
    """

    current = _read_stored_state(spawns_dir, record.id)
    if (
        current is not None
        and current.status in TERMINAL_SPAWN_STATUSES
        and not allow_terminal_overwrite
    ):
        raise ValueError(f"Refusing to overwrite terminal spawn state: {record.id}")

    if current is not None and current.cancel_intent is not None and record.cancel_intent is None:
        record = record.model_copy(update={"cancel_intent": current.cancel_intent})

    next_revision = revision if revision is not None else (current.revision + 1 if current else 1)
    stored = record_to_stored_state(record, revision=next_revision)
    atomic_write_text(
        _state_path(spawns_dir, record.id),
        stored.model_dump_json(indent=2) + "\n",
    )
    return next_revision


def write_state_locked(
    spawns_dir: Path,
    spawn_id: str,
    mutator: Callable[[SpawnRecord], SpawnRecord],
    *,
    allow_terminal_overwrite: bool = False,
) -> SpawnRecord:
    """Mutate one spawn state under its per-spawn ``state.lock``."""

    with lock_file(_lock_path(spawns_dir, spawn_id)):
        current = read_state(spawns_dir, spawn_id)
        if current is None:
            raise FileNotFoundError(_state_path(spawns_dir, spawn_id))
        updated = mutator(current)
        if updated.id != spawn_id:
            raise ValueError("Locked state mutator must not change spawn id")
        write_state(
            spawns_dir,
            updated,
            allow_terminal_overwrite=allow_terminal_overwrite,
        )
        committed = read_state(spawns_dir, spawn_id)
        if committed is None:
            raise FileNotFoundError(_state_path(spawns_dir, spawn_id))
        return committed


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
    "StoredSpawnState",
    "read_prompt",
    "read_state",
    "record_to_stored_state",
    "scan_spawn_ids",
    "stored_state_to_record",
    "write_state",
    "write_state_locked",
]
