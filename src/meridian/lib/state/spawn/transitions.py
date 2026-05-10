"""Pure spawn-state transition mutators.

These helpers contain no persistence, locking, or lifecycle hook dispatch.
Callers decide whether they are running on the owner hot path or under the
repository lock, then apply the same field-level mutation here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from meridian.lib.core.spawn_lifecycle import TERMINAL_SPAWN_STATUSES, validate_transition

if TYPE_CHECKING:
    from meridian.lib.core.domain import SpawnStatus, TokenUsage
    from meridian.lib.state.spawn.model import LaunchMode, SpawnOrigin, SpawnRecord


def apply_mark_running(
    record: SpawnRecord,
    *,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    validate_status_transition: bool = True,
) -> SpawnRecord:
    """Return ``record`` with running status and optional launch metadata."""

    if validate_status_transition and record.status not in {"unknown", "running"}:
        validate_transition(cast("SpawnStatus", record.status), "running")

    updates: dict[str, object] = {"status": "running"}
    if launch_mode is not None:
        updates["launch_mode"] = launch_mode
    if worker_pid is not None:
        updates["worker_pid"] = worker_pid
    if runner_pid is not None:
        updates["runner_pid"] = runner_pid
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
        "process_exit_code": exit_code,
        "exited_at": exited_at,
    }
    if spawn_id is not None:
        updates["id"] = spawn_id
    return record.model_copy(update=updates)


def apply_mark_finalizing(record: SpawnRecord) -> SpawnRecord:
    """Return ``record`` after validating and applying running -> finalizing."""

    validate_transition(cast("SpawnStatus", record.status), "finalizing")
    return record.model_copy(update={"status": "finalizing"})


def apply_finalize(
    record: SpawnRecord,
    status: SpawnStatus,
    exit_code: int,
    *,
    origin: SpawnOrigin,
    finished_at: str,
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
        validate_transition(record.status, status)

    return record.model_copy(
        update={
            "status": status,
            "exit_code": exit_code,
            "finished_at": finished_at,
            "duration_secs": duration_secs,
            "total_cost_usd": usage.total_cost_usd if usage is not None else None,
            "input_tokens": usage.input_tokens if usage is not None else None,
            "output_tokens": usage.output_tokens if usage is not None else None,
            "cache_read_input_tokens": (
                usage.cache_read_input_tokens if usage is not None else None
            ),
            "cache_creation_input_tokens": (
                usage.cache_creation_input_tokens if usage is not None else None
            ),
            "reasoning_tokens": usage.reasoning_tokens if usage is not None else None,
            "cost_is_estimate": (
                (usage.cost_is_estimate if usage is not None else False)
                or record.cost_is_estimate
            ),
            "error": error,
            "terminal_origin": origin,
        }
    )
