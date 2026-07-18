from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian.lib.state.spawn.model import RunnerExitFacts, SpawnRecord, TerminalFacts
from meridian.lib.state.spawn.repository import (
    read_prompt,
    read_state,
    record_to_stored_state,
    write_state_locked,
)


def _record(
    spawn_id: str = "p1",
    *,
    status: str = "running",
    prompt: str | None = "hello",
    goal: str | None = "ship persistence",
) -> SpawnRecord:
    terminal = (
        TerminalFacts(
            status=status,
            exit_code=0,
            finished_at="2026-05-01T00:01:00Z",
            published_at="2026-05-01T00:01:00Z",
            origin="runner",
        )
        if status in {"succeeded", "failed", "cancelled", "timed_out"}
        else None
    )
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
        goal=goal,
        harness_session_id="session-1",
        execution_cwd="/tmp/project",
        claude_config_dir=None,
        launch_mode="background",
        worker_pid=123,
        runner_pid=456,
        runner_created_at_epoch=None,
        status=status,
        prompt=prompt,
        started_at="2026-05-01T00:00:00Z",
        last_attempt_exited_at=None,
        last_attempt_exit_code=None,
        runner_exit=None,
        terminal=terminal,
    )


def _seed_state(spawns_dir: Path, record: SpawnRecord) -> None:
    spawn_dir = spawns_dir / record.id
    spawn_dir.mkdir(parents=True)
    stored = record_to_stored_state(record)
    (spawn_dir / "state.json").write_text(
        stored.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def test_v2_state_round_trips_without_prompt_body(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    record = _record(prompt="hello world")

    _seed_state(spawns_dir, record)

    state_text = (spawns_dir / "p1" / "state.json").read_text(encoding="utf-8")
    assert '"prompt_length": 11' in state_text
    assert "hello world" not in state_text

    stored_prompt_path = spawns_dir / "p1" / "starting-prompt.md"
    stored_prompt_path.write_text("hello world", encoding="utf-8")
    restored = read_state(spawns_dir, "p1")

    assert restored == record
    assert read_prompt(spawns_dir, "p1") == "hello world"


@pytest.mark.parametrize(
    "update",
    (
        {"status": "running"},
        {"exit_code": None},
        {"finished_at": None},
    ),
)
def test_terminal_facts_copy_revalidates_closed_status_and_required_fields(
    update: dict[str, object],
) -> None:
    facts = TerminalFacts(
        status="succeeded",
        exit_code=0,
        finished_at="2026-05-01T00:01:00Z",
        published_at="2026-05-01T00:01:00Z",
        origin="runner",
    )

    with pytest.raises(ValidationError):
        facts.model_copy(update=update)


@pytest.mark.parametrize("update", ({"status": "running"}, {"exit_code": None}))
def test_runner_exit_facts_copy_revalidates_complete_terminal_tuple(
    update: dict[str, object],
) -> None:
    facts = RunnerExitFacts(
        status="failed",
        exit_code=9,
        error="runner failed",
        exited_at="2026-05-01T00:01:00Z",
    )

    with pytest.raises(ValidationError):
        facts.model_copy(update=update)


def test_spawn_record_copy_revalidates_terminal_equivalence() -> None:
    running = _record()
    succeeded = _record(status="succeeded")
    assert succeeded.terminal is not None
    mismatched = TerminalFacts(
        status="failed",
        exit_code=1,
        finished_at="2026-05-01T00:01:00Z",
        published_at="2026-05-01T00:01:00Z",
        origin="runner",
    )

    invalid_updates = (
        (running, {"status": "succeeded"}),
        (running, {"terminal": succeeded.terminal}),
        (succeeded, {"terminal": None}),
        (succeeded, {"terminal": mismatched}),
    )
    for record, update in invalid_updates:
        with pytest.raises(ValidationError):
            record.model_copy(update=update)


def test_write_state_locked_refuses_to_overwrite_terminal_state(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    _seed_state(spawns_dir, _record(status="succeeded"))
    replacement = TerminalFacts(
        status="failed",
        exit_code=1,
        finished_at="2026-05-01T00:02:00Z",
        published_at="2026-05-01T00:02:00Z",
        origin="runner",
    )

    with pytest.raises(ValueError, match="terminal spawn state"):
        write_state_locked(
            spawns_dir,
            "p1",
            lambda current: current.model_copy(
                update={"status": "failed", "terminal": replacement}
            ),
        )

    assert read_state(spawns_dir, "p1").status == "succeeded"  # type: ignore[union-attr]


def test_write_state_locked_applies_mutator_under_per_spawn_lock(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    _seed_state(spawns_dir, _record())

    committed = write_state_locked(
        spawns_dir,
        "p1",
        lambda current: current.model_copy(update={"runner_pid": 789, "desc": "updated"}),
    )

    assert committed.runner_pid == 789
    assert committed.desc == "updated"


def test_start_spawn_persists_display_label_when_goal_and_desc_absent(tmp_path: Path) -> None:
    from meridian.lib.state.paths import RuntimePaths
    from meridian.lib.state.spawn_store import start_spawn

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    spawn_id = start_spawn(
        runtime_root,
        spawn_id="p1",
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="summarize this task please",
        desc=None,
        goal=None,
    )

    state_text = (
        RuntimePaths.from_root_dir(runtime_root).spawns_dir / str(spawn_id) / "state.json"
    ).read_text(encoding="utf-8")
    assert '"display_label": "summarize this task please"' in state_text

    spawns_dir = RuntimePaths.from_root_dir(runtime_root).spawns_dir
    restored = read_state(spawns_dir, "p1", include_prompt=False)
    assert restored is not None
    assert restored.display_label == "summarize this task please"
    assert restored.prompt is None


def test_read_state_metadata_only_skips_prompt_file(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    _seed_state(spawns_dir, _record(prompt="hello world"))

    restored = read_state(spawns_dir, "p1", include_prompt=False)

    assert restored is not None
    assert restored.prompt is None
    assert restored.id == "p1"
    assert (spawns_dir / "p1" / "starting-prompt.md").exists() is False


def test_write_state_locked_rejects_mutators_that_change_spawn_id(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    _seed_state(spawns_dir, _record())

    with pytest.raises(ValueError, match="must not change spawn id"):
        write_state_locked(
            spawns_dir,
            "p1",
            lambda current: current.model_copy(update={"id": "p2"}),
        )

    assert read_state(spawns_dir, "p1") == _record().model_copy(update={"prompt": None})
