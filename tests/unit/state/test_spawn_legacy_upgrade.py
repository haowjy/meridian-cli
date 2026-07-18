import json
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.state.spawn import repository
from meridian.lib.state.spawn.repository import (
    Applied,
    SpawnStateQuarantined,
    read_state,
    write_state_locked,
)


def _v2_flat(spawn_id: str = "p1", *, status: str = "running") -> dict[str, Any]:
    """Return the complete field set written by origin/main's v2 repository."""

    return {
        "v": 2,
        "id": spawn_id,
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


def _terminal_v2(status: str, *, exit_code: int, error: str | None) -> dict[str, Any]:
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


def _write_row(spawns_dir: Path, row: dict[str, Any]) -> Path:
    state_path = spawns_dir / row["id"] / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(row), encoding="utf-8")
    return state_path


@pytest.mark.parametrize(
    ("row", "expected_error"),
    [
        (_v2_flat(), None),
        (_terminal_v2("succeeded", exit_code=0, error=None), None),
        (_terminal_v2("failed", exit_code=9, error="backend failed"), "backend failed"),
    ],
)
def test_v2_flat_rows_parse_as_strict_v3_models(
    tmp_path: Path, row: dict[str, Any], expected_error: str | None
) -> None:
    spawns_dir = tmp_path / "spawns"
    state_path = _write_row(spawns_dir, row)

    restored = read_state(spawns_dir, "p1", include_prompt=False)

    assert restored is not None
    assert restored.status == row["status"]
    if row["runner_exit_status"] is None:
        assert restored.runner_exit is None
    else:
        assert restored.runner_exit is not None
        assert restored.runner_exit.status == row["runner_exit_status"]
        assert restored.runner_exit.exit_code == row["runner_exit_code"]
        assert restored.runner_exit.exited_at == row["runner_exit_at"]
    if row["status"] == "running":
        assert restored.terminal is None
    else:
        assert restored.terminal is not None
        assert restored.terminal.exit_code == row["exit_code"]
        assert restored.terminal.finished_at == row["finished_at"]
        assert restored.terminal.published_at == row["published_at"]
        assert restored.terminal.error == expected_error
    assert json.loads(state_path.read_text(encoding="utf-8"))["v"] == 2


def test_v2_nested_terminal_status_agreement_upgrades(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    row = _terminal_v2("succeeded", exit_code=0, error=None)
    terminal = {
        "status": row["status"],
        "exit_code": row["exit_code"],
        "finished_at": row["finished_at"],
        "published_at": row["published_at"],
        "duration_secs": row["duration_secs"],
        "total_cost_usd": row["total_cost_usd"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cache_read_input_tokens": row["cache_read_input_tokens"],
        "cache_creation_input_tokens": row["cache_creation_input_tokens"],
        "reasoning_tokens": row["reasoning_tokens"],
        "cost_is_estimate": row["cost_is_estimate"],
        "error": row["error"],
        "origin": row["terminal_origin"],
    }
    for field in (
        "finished_at", "published_at", "exit_code", "duration_secs", "total_cost_usd",
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "reasoning_tokens", "cost_is_estimate", "error",
        "terminal_origin",
    ):
        row.pop(field)
    row["runner_exit"] = {
        "status": row.pop("runner_exit_status"),
        "exit_code": row.pop("runner_exit_code"),
        "error": row.pop("runner_exit_error"),
        "exited_at": row.pop("runner_exit_at"),
    }
    row["terminal"] = terminal
    _write_row(spawns_dir, row)

    restored = read_state(spawns_dir, "p1", include_prompt=False)

    assert restored is not None
    assert restored.terminal is not None
    assert restored.terminal.exit_code == 0


def test_missing_version_dispatches_through_v2_upgrade(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    row = _v2_flat()
    row.pop("v")
    _write_row(spawns_dir, row)

    restored = read_state(spawns_dir, "p1", include_prompt=False)

    assert restored is not None
    assert restored.status == "running"


def test_retired_revision_field_is_deliberately_dropped(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    row = _v2_flat()
    row["revision"] = 17
    state_path = _write_row(spawns_dir, row)

    restored = read_state(spawns_dir, "p1", include_prompt=False)

    assert restored is not None
    assert restored.status == "running"
    assert json.loads(state_path.read_text(encoding="utf-8"))["revision"] == 17


def test_terminal_row_without_published_at_backfills_finish_time(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    row = _terminal_v2("succeeded", exit_code=0, error=None)
    row.pop("published_at")
    _write_row(spawns_dir, row)

    restored = read_state(spawns_dir, "p1", include_prompt=False)

    assert restored is not None
    assert restored.terminal is not None
    assert restored.terminal.published_at == restored.terminal.finished_at


def test_terminal_row_without_finish_or_publication_time_quarantines(
    tmp_path: Path,
) -> None:
    spawns_dir = tmp_path / "spawns"
    row = _terminal_v2("succeeded", exit_code=0, error=None)
    row.pop("finished_at")
    row.pop("published_at")
    _write_row(spawns_dir, row)

    with pytest.raises(SpawnStateQuarantined) as quarantined:
        read_state(spawns_dir, "p1")

    errors = quarantined.value.report.validation_errors
    assert "incomplete_terminal" in str(errors)
    assert "finished_at" in str(errors)


def test_unrecognized_legacy_field_still_quarantines(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    row = _v2_flat()
    row["not_a_curated_retired_field"] = "must not disappear"
    _write_row(spawns_dir, row)

    with pytest.raises(SpawnStateQuarantined) as quarantined:
        read_state(spawns_dir, "p1")

    errors = quarantined.value.report.validation_errors
    assert "unknown_fields" in str(errors)
    assert "not_a_curated_retired_field" in str(errors)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda row: row.update(unknown_legacy_fact="x"), "unknown"),
        (lambda row: row.update(exit_code=3), "non-terminal"),
        (lambda row: row.update(runner_exit_status="failed"), "runner"),
    ],
)
def test_ambiguous_v2_flat_rows_quarantine_with_reason(
    tmp_path: Path, mutate: Any, reason: str
) -> None:
    spawns_dir = tmp_path / "spawns"
    row = _v2_flat()
    mutate(row)
    _write_row(spawns_dir, row)

    with pytest.raises(SpawnStateQuarantined) as quarantined:
        read_state(spawns_dir, "p1")

    assert reason in str(quarantined.value.report.validation_errors).lower()


def test_disagreeing_nested_terminal_status_quarantines_with_reason(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    row = _v2_flat(status="succeeded")
    row["terminal"] = {
        "status": "failed",
        "exit_code": 1,
        "finished_at": "2026-07-01T00:02:00Z",
        "published_at": "2026-07-01T00:02:01Z",
        "origin": "runner",
    }
    _write_row(spawns_dir, row)

    with pytest.raises(SpawnStateQuarantined) as quarantined:
        read_state(spawns_dir, "p1")

    assert "disagree" in str(quarantined.value.report.validation_errors).lower()


def test_nested_terminal_facts_on_active_status_quarantine(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    row = _v2_flat()
    projection_fields = {
        "runner_exit_code", "runner_exit_status", "runner_exit_error", "runner_exit_at",
        "finished_at", "published_at", "exit_code", "duration_secs", "total_cost_usd",
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "reasoning_tokens", "cost_is_estimate", "error",
        "terminal_origin",
    }
    for field in set(row) & projection_fields:
        row.pop(field)
    row.update(
        runner_exit=None,
        terminal={
            "status": "running",
            "exit_code": 0,
            "finished_at": "2026-07-01T00:02:00Z",
            "published_at": "2026-07-01T00:02:01Z",
            "origin": "runner",
        },
    )
    _write_row(spawns_dir, row)

    with pytest.raises(SpawnStateQuarantined) as quarantined:
        read_state(spawns_dir, "p1")

    assert "terminal status and terminal facts" in str(
        quarantined.value.report.validation_errors
    )


def test_v3_rows_bypass_legacy_upgrader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawns_dir = tmp_path / "spawns"
    row = _v2_flat()
    row["v"] = 3
    for field in (
        "runner_exit_code", "runner_exit_status", "runner_exit_error", "runner_exit_at",
        "finished_at", "published_at", "exit_code", "duration_secs", "total_cost_usd",
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "reasoning_tokens", "cost_is_estimate", "error",
        "terminal_origin",
    ):
        row.pop(field)
    row.update(runner_exit=None, terminal=None)
    _write_row(spawns_dir, row)

    def fail_if_called(_raw: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("v3 must bypass the legacy upgrader")

    monkeypatch.setattr(repository, "upgrade_legacy_spawn_state", fail_if_called)

    assert read_state(spawns_dir, "p1", include_prompt=False) is not None


def test_mutating_legacy_row_writes_v3_without_rewrite_on_read(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    state_path = _write_row(spawns_dir, _v2_flat())

    assert read_state(spawns_dir, "p1", include_prompt=False) is not None
    assert json.loads(state_path.read_text(encoding="utf-8"))["v"] == 2

    outcome = write_state_locked(
        spawns_dir,
        "p1",
        lambda current: current.model_copy(update={"desc": "mutated"}),
    )

    assert isinstance(outcome, Applied)
    assert json.loads(state_path.read_text(encoding="utf-8"))["v"] == 3
