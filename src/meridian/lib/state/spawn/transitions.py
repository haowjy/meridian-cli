"""Pure spawn-state transition mutators.

These helpers contain no persistence, locking, or lifecycle hook dispatch.
The repository applies these field-level mutations to a fresh snapshot while
holding the per-spawn lock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES, validate_transition

if TYPE_CHECKING:
    from meridian.lib.core.domain import TokenUsage
    from meridian.lib.state.spawn.model import (
        CancelIntent,
        LaunchMode,
        SpawnOrigin,
        SpawnRecord,
        TerminalSpawnStatus,
    )


def apply_mark_running(
    record: SpawnRecord,
    *,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    runner_created_at_epoch: float | None = None,
    validate_status_transition: bool = True,
) -> SpawnRecord:
    """Return ``record`` with running status and optional launch metadata."""

    if validate_status_transition and record.status not in {"unknown", "running"}:
        validate_transition(cast("SpawnStatus", record.status), SpawnStatus.RUNNING)

    updates: dict[str, object] = {"status": SpawnStatus.RUNNING}
    if launch_mode is not None:
        updates["launch_mode"] = launch_mode
    if worker_pid is not None:
        updates["worker_pid"] = worker_pid
    if runner_pid is not None:
        updates["runner_pid"] = runner_pid
        updates["runner_created_at_epoch"] = runner_created_at_epoch
    elif runner_created_at_epoch is not None:
        updates["runner_created_at_epoch"] = runner_created_at_epoch
    return record.model_copy(update=updates)


def apply_record_exited(
    record: SpawnRecord,
    *,
    exit_code: int,
    exited_at: str,
    spawn_id: str | None = None,
) -> SpawnRecord:
    """Return ``record`` with process-exit metadata applied."""

    updates: dict[str, object] = {
        "last_attempt_exit_code": exit_code,
        "last_attempt_exited_at": exited_at,
    }
    if spawn_id is not None:
        updates["id"] = spawn_id
    return record.model_copy(update=updates)


def apply_runner_exit(
    record: SpawnRecord,
    *,
    status: TerminalSpawnStatus,
    exit_code: int,
    error: str | None,
    exited_at: str,
    spawn_id: str | None = None,
) -> SpawnRecord:
    """Return ``record`` with runner-resolved terminal intent applied."""

    from meridian.lib.state.spawn.model import RunnerExitFacts

    updates: dict[str, object] = {
        "runner_exit": RunnerExitFacts(
            status=status,
            exit_code=exit_code,
            error=error,
            exited_at=exited_at,
        )
    }
    if spawn_id is not None:
        updates["id"] = spawn_id
    return record.model_copy(update=updates)


def apply_cancel_intent(
    record: SpawnRecord,
    *,
    intent: CancelIntent,
) -> SpawnRecord:
    """Return ``record`` with durable spawn-level cancel intent applied."""

    if record.status in TERMINAL_SPAWN_STATUSES:
        return record
    if record.cancel_intent is not None:
        return record
    return record.model_copy(update={"cancel_intent": intent})


def apply_mark_finalizing(record: SpawnRecord) -> SpawnRecord:
    """Return ``record`` after validating and applying running -> finalizing."""

    validate_transition(cast("SpawnStatus", record.status), SpawnStatus.FINALIZING)
    return record.model_copy(update={"status": SpawnStatus.FINALIZING})


def apply_finalize(
    record: SpawnRecord,
    status: TerminalSpawnStatus,
    exit_code: int,
    *,
    origin: SpawnOrigin,
    finished_at: str,
    published_at: str,
    duration_secs: float | None = None,
    usage: TokenUsage | None = None,
    error: str | None = None,
    validate_status_transition: bool = True,
) -> SpawnRecord:
    """Return ``record`` with terminal status and metrics applied."""

    if (
        validate_status_transition
        and record.status != "unknown"
        and record.status not in TERMINAL_SPAWN_STATUSES
    ):
        validate_transition(record.status, SpawnStatus(status))

    from meridian.lib.state.spawn.model import TerminalFacts

    terminal = TerminalFacts(
        exit_code=exit_code,
        finished_at=finished_at,
        published_at=published_at,
        duration_secs=duration_secs,
        total_cost_usd=usage.total_cost_usd if usage is not None else None,
        input_tokens=usage.input_tokens if usage is not None else None,
        output_tokens=usage.output_tokens if usage is not None else None,
        cache_read_input_tokens=usage.cache_read_input_tokens if usage is not None else None,
        cache_creation_input_tokens=(
            usage.cache_creation_input_tokens if usage is not None else None
        ),
        reasoning_tokens=usage.reasoning_tokens if usage is not None else None,
        cost_is_estimate=(
            (usage.cost_is_estimate if usage is not None else False)
            or (record.terminal.cost_is_estimate if record.terminal is not None else False)
        ),
        error=error,
        origin=origin,
    )
    return record.model_copy(update={"status": SpawnStatus(status), "terminal": terminal})
