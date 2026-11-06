"""Strict in-memory acceptance and quarantine matrix for legacy spawn rows."""

from __future__ import annotations

from typing import Any

import pytest

from meridian.lib.state.spawn.legacy import (
    LegacySpawnStateUpgradeError,
    upgrade_legacy_spawn_state,
)
from meridian.lib.state.spawn.repository import StoredSpawnState


def _v2_flat(*, status: str = "running") -> dict[str, Any]:
    return {
        "v": 2,
        "id": "p1",
        "chat_id": "c1",
        "owner_chat_id": None,
        "parent_id": None,
        "originating_bash_id": None,
        "model": "gpt-5.4",
        "agent": "coder",
        "agent_path": None,
        "skills": ["dev-principles"],
        "skill_paths": ["/tmp/dev-principles"],
        "harness": "codex",
        "kind": "child",
        "desc": "legacy row",
        "work_id": "work-1",
        "goal": "upgrade state",
        "display_label": "upgrade state",
        "harness_session_id": "thread-1",
        "control_root": "/tmp/control",
        "task_cwd": "/tmp/task",
        "execution_cwd": "/tmp/task",
        "claude_config_dir": None,
        "launch_mode": "background",
        "worker_pid": 101,
        "runner_pid": 102,
        "runner_created_at_epoch": 1_752_000_000.0,
        "resident_rearm_count": 0,
        "status": status,
        "started_at": "2026-07-01T00:00:00Z",
        "last_attempt_exited_at": None,
        "last_attempt_exit_code": None,
        "runner_exit_code": None,
        "runner_exit_status": None,
        "runner_exit_error": None,
        "runner_exit_at": None,
        "cancel_intent": None,
        "finished_at": None,
        "published_at": None,
        "exit_code": None,
        "duration_secs": None,
        "total_cost_usd": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "reasoning_tokens": None,
        "cost_is_estimate": False,
        "error": None,
        "terminal_origin": None,
        "prompt_length": 12,
        "launch_policy_snapshot": None,
    }


def _terminal_v2(status: str, exit_code: int, error: str | None) -> dict[str, Any]:
    row = _v2_flat(status=status)
    row.update(
        runner_exit_code=exit_code,
        runner_exit_status=status,
        runner_exit_error=error,
        runner_exit_at="2026-07-01T00:01:59Z",
        finished_at="2026-07-01T00:02:00Z",
        published_at="2026-07-01T00:02:01Z",
        exit_code=exit_code,
        duration_secs=120.0,
        total_cost_usd=0.25,
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=5,
        cache_creation_input_tokens=3,
        reasoning_tokens=7,
        error=error,
        terminal_origin="runner",
    )
    return row


def _upgrade(row: dict[str, Any]) -> StoredSpawnState:
    return StoredSpawnState.model_validate(upgrade_legacy_spawn_state(row))


def test_terminal_flat_rows_preserve_success_and_failure_facts() -> None:
    for status, exit_code, error in (
        ("succeeded", 0, None),
        ("failed", 9, "backend failed"),
    ):
        restored = _upgrade(_terminal_v2(status, exit_code, error))

        assert restored.status == status
        assert restored.runner_exit is not None
        assert restored.runner_exit.status == status
        assert restored.runner_exit.exit_code == exit_code
        assert restored.runner_exit.exited_at == "2026-07-01T00:01:59Z"
        assert restored.terminal is not None
        assert restored.terminal.exit_code == exit_code
        assert restored.terminal.finished_at == "2026-07-01T00:02:00Z"
        assert restored.terminal.published_at == "2026-07-01T00:02:01Z"
        assert restored.terminal.total_cost_usd == 0.25
        assert restored.terminal.error == error


def test_unknown_legacy_field_is_rejected_without_data_loss() -> None:
    row = _v2_flat()
    row["not_a_curated_retired_field"] = "must not disappear"

    with pytest.raises(LegacySpawnStateUpgradeError, match="unknown_fields"):
        upgrade_legacy_spawn_state(row)


