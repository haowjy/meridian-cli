"""Write the row shapes older builds left behind (`finalizing`, exit facts, cancel intent).

This build writes none of them, but it still reads and honours each, so tests that cover
that read path seed the rows here.
"""

from __future__ import annotations

from pathlib import Path

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.state.spawn.model import CancelIntent, RunnerExitFacts, SpawnRecord
from meridian.lib.state.spawn.repository import write_state_locked


def _update(runtime_root: Path, spawn_id: str, **fields: object) -> None:
    def mutate(current: SpawnRecord) -> SpawnRecord:
        return current.model_copy(update=fields)

    write_state_locked(runtime_root / "spawns", str(spawn_id), mutate)


def mark_finalizing(runtime_root: Path, spawn_id: str) -> None:
    _update(runtime_root, spawn_id, status=SpawnStatus.FINALIZING)


def record_exited(runtime_root: Path, spawn_id: str, *, exit_code: int, exited_at: str) -> None:
    _update(
        runtime_root,
        spawn_id,
        last_attempt_exit_code=exit_code,
        last_attempt_exited_at=exited_at,
    )


def record_runner_exit(
    runtime_root: Path,
    spawn_id: str,
    *,
    status: str,
    exit_code: int,
    error: str | None = None,
    exited_at: str,
) -> None:
    _update(
        runtime_root,
        spawn_id,
        runner_exit=RunnerExitFacts(
            status=status, exit_code=exit_code, error=error, exited_at=exited_at
        ),
    )


def record_cancel_intent(
    runtime_root: Path,
    spawn_id: str,
    *,
    exit_code: int = 130,
    error: str | None = "cancelled",
    requested_at: str = "2026-01-01T00:00:00Z",
) -> None:
    _update(
        runtime_root,
        spawn_id,
        cancel_intent=CancelIntent(requested_at=requested_at, exit_code=exit_code, error=error),
    )
