from pathlib import Path

import pytest

from meridian.lib.state.spawn.repository import (
    read_prompt,
    read_state,
    scan_spawn_ids,
    write_state,
    write_state_locked,
)
from meridian.lib.state.spawn_store import SpawnRecord


def _record(
    spawn_id: str = "p1", *, status: str = "running", prompt: str | None = "hello"
) -> SpawnRecord:
    return SpawnRecord(
        id=spawn_id,
        chat_id="c1",
        parent_id=None,
        model="gpt-5.4",
        agent="coder",
        agent_path=None,
        skills=("dev-principles",),
        skill_paths=("/tmp/dev-principles",),
        harness="codex",
        kind="child",
        desc="test spawn",
        work_id="work-1",
        harness_session_id="session-1",
        execution_cwd="/tmp/project",
        claude_config_dir=None,
        launch_mode="background",
        worker_pid=123,
        runner_pid=456,
        status=status,
        prompt=prompt,
        started_at="2026-05-01T00:00:00Z",
        exited_at=None,
        process_exit_code=None,
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
    )


def test_v2_state_round_trips_without_prompt_body(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    record = _record(prompt="hello world")

    revision = write_state(spawns_dir, record)

    assert revision == 1
    state_text = (spawns_dir / "p1" / "state.json").read_text(encoding="utf-8")
    assert '"revision": 1' in state_text
    assert '"prompt_length": 11' in state_text
    assert "hello world" not in state_text

    stored_prompt_path = spawns_dir / "p1" / "starting-prompt.md"
    stored_prompt_path.write_text("hello world", encoding="utf-8")
    restored = read_state(spawns_dir, "p1")

    assert restored == record
    assert read_prompt(spawns_dir, "p1") == "hello world"


def test_scan_spawn_ids_lists_only_directories_with_state(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    write_state(spawns_dir, _record("opaque-id"))
    write_state(spawns_dir, _record("r7"))
    (spawns_dir / "empty").mkdir()
    (spawns_dir / "file").write_text("not a spawn", encoding="utf-8")

    assert scan_spawn_ids(spawns_dir) == ["opaque-id", "r7"]


def test_write_state_refuses_to_overwrite_terminal_state(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    write_state(spawns_dir, _record(status="succeeded"))

    with pytest.raises(ValueError, match="terminal spawn state"):
        write_state(spawns_dir, _record(status="running"))

    assert read_state(spawns_dir, "p1").status == "succeeded"  # type: ignore[union-attr]


def test_write_state_locked_applies_mutator_under_per_spawn_lock(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    write_state(spawns_dir, _record())

    committed = write_state_locked(
        spawns_dir,
        "p1",
        lambda current: current.model_copy(update={"runner_pid": 789, "desc": "updated"}),
    )

    assert committed.runner_pid == 789
    assert committed.desc == "updated"
    assert '"revision": 2' in (spawns_dir / "p1" / "state.json").read_text(encoding="utf-8")
