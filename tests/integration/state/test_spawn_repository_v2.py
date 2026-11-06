import json
from pathlib import Path

import pytest

from meridian.lib.state.spawn.dogfood_migration import migrate_dogfood_spawn_rows
from meridian.lib.state.spawn.model import RunBoundaryOutcome, SpawnRecord, TerminalFacts
from meridian.lib.state.spawn.repository import (
    SpawnStateQuarantined,
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


def test_running_dogfood_migration_preserves_old_runner_row_shape(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    spawn_dir = spawns_dir / "p1"
    spawn_dir.mkdir(parents=True)
    # Field names from the PR 1 model at 77b8bc58:src/meridian/lib/state/spawn/model.py.
    pr1_field_names = frozenset(
        {
            "id",
            "history_id",
            "record_mode",
            "session_instance_id",
            "parent_history_id",
            "owner_history_id",
            "forked_from_history_id",
            "retained_history_ids",
            "state_revision",
            "chat_id",
            "entry_chat_id",
            "exit_chat_id",
            "exit_identity",
            "owner_chat_id",
            "parent_id",
            "originating_bash_id",
            "model",
            "agent",
            "agent_path",
            "skills",
            "skill_paths",
            "harness",
            "kind",
            "desc",
            "work_id",
            "goal",
            "display_label",
            "harness_session_id",
            "trampoline_successor_id",
            "control_root",
            "task_cwd",
            "execution_cwd",
            "claude_config_dir",
            "launch_mode",
            "worker_pid",
            "runner_pid",
            "runner_created_at_epoch",
            "resident_rearm_count",
            "status",
            "started_at",
            "last_attempt_exited_at",
            "last_attempt_exit_code",
            "runner_exit",
            "cancel_intent",
            "terminal",
            "launch_policy_snapshot",
            "prompt",
        }
    )
    raw = {
        "v": 3,
        "id": "p1",
        "entry_chat_id": "c-entry",
        "exit_chat_id": None,
        "exit_identity": None,
        "trampoline_successor_id": None,
        "kind": "child",
        "status": "running",
    }
    state_path = spawn_dir / "state.json"
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    assert migrate_dogfood_spawn_rows(tmp_path).migrated == ("p1",)

    migrated = json.loads(state_path.read_text(encoding="utf-8"))
    assert migrated.keys() == (
        raw.keys() - {"entry_chat_id", "exit_chat_id", "exit_identity", "trampoline_successor_id"}
    ) | {"chat_id"}
    assert migrated["chat_id"] == "c-entry"
    assert migrated["kind"] == "child"
    assert "run_boundary" not in migrated
    # ``v`` belongs to the persisted-row envelope, not PR 1's SpawnStateFields.
    assert (migrated.keys() - {"v"}) <= pr1_field_names


def test_dogfood_boundary_rows_quarantine_until_migrated_once(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    _seed_state(spawns_dir, _record(status="succeeded"))
    _seed_state(spawns_dir, _record("p2", status="succeeded"))
    state_path = spawns_dir / "p1" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["entry_chat_id"] = "c-entry"
    state["exit_chat_id"] = "c-exit"
    state["exit_identity"] = "verified"
    state.pop("run_boundary", None)
    state["trampoline_successor_id"] = "diagnostic-only"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(SpawnStateQuarantined, match="run `meridian doctor` to migrate it"):
        read_state(spawns_dir, "p1", include_prompt=False)

    assert migrate_dogfood_spawn_rows(tmp_path).migrated == ("p1",)
    assert migrate_dogfood_spawn_rows(tmp_path).migrated == ()

    loaded = read_state(spawns_dir, "p1", include_prompt=False)
    assert loaded is not None
    assert loaded.chat_id == "c1"
    assert loaded.run_boundary == RunBoundaryOutcome(
        status="verified", trampoline_successor_id="diagnostic-only"
    )
    assert loaded.current_chat_id == loaded.continue_chat_id == "c-exit"
    assert "exit_identity" not in state_path.read_text(encoding="utf-8")


def test_dogfood_migration_isolates_malformed_rows(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    for spawn_id in ("p1", "p2"):
        _seed_state(spawns_dir, _record(spawn_id, status="succeeded"))
    invalid_path = spawns_dir / "p1" / "state.json"
    invalid = json.loads(invalid_path.read_text(encoding="utf-8"))
    invalid.update(exit_identity="bogus-status", exit_chat_id="c9")
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    valid_path = spawns_dir / "p2" / "state.json"
    valid = json.loads(valid_path.read_text(encoding="utf-8"))
    valid.update(entry_chat_id="c2", exit_identity="verified", exit_chat_id="c3")
    valid_path.write_text(json.dumps(valid), encoding="utf-8")
    (spawns_dir / "p0").mkdir()
    (spawns_dir / "p0" / "state.json").write_text(
        '{"v":3,"id":"p0","entry_chat_id": "c', encoding="utf-8"
    )

    first = migrate_dogfood_spawn_rows(tmp_path)
    second = migrate_dogfood_spawn_rows(tmp_path)

    assert first.migrated == ("p2",)
    assert [spawn_id for spawn_id, _ in first.failed] == ["p0", "p1"]
    assert "JSONDecodeError" in first.failed[0][1]
    assert "ValidationError" in first.failed[1][1]
    assert second.migrated == ()
    assert [spawn_id for spawn_id, _ in second.failed] == ["p0", "p1"]
    loaded = read_state(spawns_dir, "p2", include_prompt=False)
    assert loaded is not None
    assert loaded.continue_chat_id == "c3"


@pytest.mark.parametrize(
    ("status", "exit_id", "current_id"),
    [
        ("unresolved", "c2", None),
        ("verified", None, None),
        ("verified", 42, "c3"),
        ("verified", "c2", 42),
    ],
)
def test_legacy_exit_normalization_rejects_invalid_evidence(
    tmp_path: Path,
    status: str,
    exit_id: object,
    current_id: object,
) -> None:
    for flat in (False, True):
        runtime = tmp_path / ("dogfood" if flat else "released")
        spawns_dir = runtime / "spawns"
        _seed_state(spawns_dir, _record(status="succeeded"))
        path = spawns_dir / "p1/state.json"
        state = json.loads(path.read_text())
        state["current_chat_id"] = current_id
        if flat:
            state.update(exit_identity=status, exit_chat_id=exit_id)
        else:
            state["run_boundary"] = {"status": status, "exit_chat_id": exit_id}
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        if flat:
            result = migrate_dogfood_spawn_rows(runtime)
            assert result.migrated == ()
            assert [spawn_id for spawn_id, _ in result.failed] == ["p1"]
        else:
            with pytest.raises(SpawnStateQuarantined):
                read_state(spawns_dir, "p1", include_prompt=False)
        assert path.read_bytes() == before


def test_write_state_locked_refuses_to_overwrite_terminal_state(tmp_path: Path) -> None:
    spawns_dir = tmp_path / "spawns"
    _seed_state(spawns_dir, _record(status="succeeded"))
    replacement = TerminalFacts(
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


