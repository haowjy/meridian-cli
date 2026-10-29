"""Pure spawn-state transition mutators.

These helpers contain no persistence, locking, or lifecycle hook dispatch.
The repository applies these field-level mutations to a fresh snapshot while
holding the per-spawn lock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meridian.lib.core.domain import SpawnStatus

if TYPE_CHECKING:
    from meridian.lib.core.domain import TokenUsage
    from meridian.lib.state.spawn.model import (
        LaunchMode,
        SpawnOrigin,
        SpawnRecord,
        TerminalSpawnStatus,
    )


def apply_launch_facts(
    record: SpawnRecord,
    *,
    launch_mode: LaunchMode | None = None,
    worker_pid: int | None = None,
    runner_pid: int | None = None,
    runner_created_at_epoch: float | None = None,
) -> SpawnRecord:
    """Return ``record`` with launch facts applied. Rows are created ``running``."""

    updates: dict[str, object] = {}
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
    managed_scopes_pending: bool = False,
) -> SpawnRecord:
    """Return ``record`` with terminal status and metrics applied."""

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
        harness_cumulative=usage.harness_cumulative if usage is not None else None,
        error=error,
        origin=origin,
        managed_scopes_pending=managed_scopes_pending,
    )
    return record.model_copy(update={"status": SpawnStatus(status), "terminal": terminal})
