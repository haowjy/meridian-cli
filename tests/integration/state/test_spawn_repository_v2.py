import json
import os
from pathlib import Path

import psutil
import pytest

from meridian.lib.state.spawn.legacy_rows import MARKER, repair_legacy_spawn_rows
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


def _rewritten(runtime_root: Path) -> list[str]:
    return [row.spawn_id for row in repair_legacy_spawn_rows(runtime_root).rewritten]


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

    # Active with no launch lock: the row is rewritten but keeps no lock.
    assert [row.spawn_id for row in repair_legacy_spawn_rows(tmp_path).unsettled] == ["p1"]

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

    assert _rewritten(tmp_path) == ["p1", "p2"]  # p2 only gains its launch lock
    assert _rewritten(tmp_path) == []

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

    first = repair_legacy_spawn_rows(tmp_path)
    second = repair_legacy_spawn_rows(tmp_path)

    assert [row.spawn_id for row in first.rewritten] == ["p2"]
    assert [row.spawn_id for row in first.failed] == ["p0", "p1"]
    assert all(row.dogfood for row in first.failed)
    assert "JSONDecodeError" in first.failed[0].reason
    assert "ValidationError" in first.failed[1].reason
    assert second.rewritten == ()
    assert [row.spawn_id for row in second.failed] == ["p0", "p1"]
    # No later pass would read them either, so they leave the marker to doctor.
    assert (tmp_path / MARKER).exists()
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
            result = repair_legacy_spawn_rows(runtime)
            assert result.rewritten == ()
            assert [row.spawn_id for row in result.failed] == ["p1"]
        else:
            with pytest.raises(SpawnStateQuarantined):
                read_state(spawns_dir, "p1", include_prompt=False)
        assert path.read_bytes() == before


def test_legacy_row_repair_finishes_after_a_crash_and_marks_only_a_clean_pass(
    tmp_path: Path,
) -> None:
    spawns_dir = tmp_path / "spawns"
    # p1: v2 from 0.3.42 or older. p2: the state a crash between rewriting a row and
    # creating its launch lock leaves. p3: a lockless row whose old runner is alive.
    # p4: an active lockless row whose runner is gone.
    v2_path = spawns_dir / "p1" / "state.json"
    v2_path.parent.mkdir(parents=True)
    v2 = {
        "v": 2,
        "id": "p1",
        "chat_id": "c1",
        "model": "gpt-5.4",
        "agent": "coder",
        "harness": "codex",
        "kind": "child",
        "runner_pid": 456,
        "status": "succeeded",
        "started_at": "2026-05-01T00:00:00Z",
        # A key this build ignores but 0.3.43 to 0.10.x read.
        "launch_policy_snapshot": {"model": "gpt-5.4", "harness": "codex", "schema_version": 1},
        **{f"runner_exit_{name}": None for name in ("code", "status", "error", "at")},
        "exit_code": 0,
        "finished_at": "2026-05-01T00:01:00Z",
        "terminal_origin": "runner",
        "cost_is_estimate": False,
        **dict.fromkeys(
            (
                "duration_secs",
                "total_cost_usd",
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "reasoning_tokens",
                "error",
            )
        ),
    }
    v2_path.write_text(json.dumps(v2), encoding="utf-8")
    before = read_state(spawns_dir, "p1", include_prompt=False)
    _seed_state(spawns_dir, _record("p2", status="succeeded"))
    _seed_state(spawns_dir, _record("p4"))
    live = _record("p3", status="succeeded").model_copy(
        update={
            "runner_pid": os.getpid(),
            "runner_created_at_epoch": psutil.Process().create_time(),
        }
    )
    _seed_state(spawns_dir, live)
    live_bytes = (spawns_dir / "p3" / "state.json").read_bytes()

    first = repair_legacy_spawn_rows(tmp_path)

    assert [row.spawn_id for row in first.rewritten] == ["p1", "p2"]
    assert [row.spawn_id for row in first.deferred] == ["p3"]
    assert [row.spawn_id for row in first.unsettled] == ["p4"]
    assert (spawns_dir / "p3" / "state.json").read_bytes() == live_bytes
    assert not (spawns_dir / "p3" / "launch.lock").exists()
    assert not (tmp_path / MARKER).exists()
    rewritten_v2 = json.loads(v2_path.read_text(encoding="utf-8"))
    assert rewritten_v2["v"] == 3
    assert rewritten_v2["launch_policy_snapshot"]["schema_version"] == 1
    assert read_state(spawns_dir, "p1", include_prompt=False) == before

    # The old runner exits.
    dead = json.loads(live_bytes)
    dead["runner_created_at_epoch"] = 1.0
    (spawns_dir / "p3" / "state.json").write_text(json.dumps(dead), encoding="utf-8")
    second = repair_legacy_spawn_rows(tmp_path)

    assert [row.spawn_id for row in second.rewritten] == ["p3"]
    assert second.done
    assert (tmp_path / MARKER).exists()
    assert all((spawns_dir / spawn_id / "launch.lock").exists() for spawn_id in ("p1", "p2", "p3"))
    assert not (spawns_dir / "p4" / "launch.lock").exists()
    assert repair_legacy_spawn_rows(tmp_path).rewritten == ()


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
