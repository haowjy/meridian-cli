# qa-validated: test-suite-redesign

"""CRUD operation tests for the spawn store.

Covers: start, get, list, update, next_id, reserve, mark_running, and
attempt-exit and runner-exit metadata writes. Finalization and concurrency live in
test_spawn_store_finalize.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.core.domain import SkillContent
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.spawn_start import SpawnStartMetadata
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.repository import read_state
from meridian.lib.state.spawn_store import (
    finalize_spawn,
    get_spawn,
    list_spawns,
    next_spawn_id,
    record_cancel_intent,
    record_runner_exit,
    record_spawn_exited,
    reserve_spawn_id,
    start_spawn,
    update_spawn,
)


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _start_test_spawn(runtime_root: Path) -> str:
    return str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
        )
    )


def test_start_and_update_project_fields_round_trip(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            launch_mode="app",
            runner_pid=1111,
        )
    )

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.launch_mode == "app"
    assert row.runner_pid == 1111

    update_spawn(runtime_root, spawn_id, launch_mode="foreground", runner_pid=2222)
    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.launch_mode == "foreground"
    assert row.runner_pid == 2222


def test_start_spawn_persists_control_root_and_task_cwd(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    control_root = (tmp_path / "control-root").resolve()
    task_cwd = (tmp_path / "task-cwd").resolve()
    control_root.mkdir(parents=True, exist_ok=True)
    task_cwd.mkdir(parents=True, exist_ok=True)

    spawn_id = str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            control_root=control_root.as_posix(),
            task_cwd=task_cwd.as_posix(),
            execution_cwd=task_cwd.as_posix(),
        )
    )

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.control_root == control_root.as_posix()
    assert row.task_cwd == task_cwd.as_posix()
    assert row.execution_cwd == task_cwd.as_posix()


def test_start_spawn_persists_goal_from_start_metadata(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            metadata=SpawnStartMetadata(
                desc="goal test",
                work_id="  w-goal  ",
                goal="  keep scope tight  ",
            ),
        )
    )

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.desc == "goal test"
    assert row.work_id == "w-goal"
    assert row.goal == "keep scope tight"


def test_start_spawn_persists_launch_policy_snapshot(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness="claude",
        agent="coder",
        skills=("testing-principles",),
        loaded_skills=(
            SkillContent(
                name="testing-principles",
                description="testing-principles skill",
                path="/skills/testing-principles/SKILL.md",
                content="# testing-principles\n\nBe consistent.\n",
                skill_type="reference",
            ),
        ),
        execution_policy=ResolvedExecutionPolicy(
            approval="auto",
            sandbox="workspace-write",
            effort="high",
        ),
        tools={"write": "allow"},
        mcp_tools=("github",),
        extra_args=("--append-system-prompt", "stable"),
        terminal_surface_mode="pty_mediated",
        matched_policy_rule="profile-default",
        model_selection_requested_token="sonnet",
        model_selection_canonical_id="claude-sonnet-4-6",
        model_selection_harness_provenance="alias-default",
        fallback_chain=({"source": "snapshot"},),
    )

    spawn_id = str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="claude-sonnet-4-6",
            agent="coder",
            harness="claude",
            prompt="hello",
            launch_policy_snapshot=snapshot,
        )
    )

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.launch_policy_snapshot == snapshot


def test_start_spawn_embeds_launch_policy_snapshot_in_state_json(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness="claude",
        loaded_skills=(
            SkillContent(
                name="testing-principles",
                description="testing-principles skill",
                path="/skills/testing-principles/SKILL.md",
                content="# testing-principles\n\nBe consistent.\n",
                skill_type="reference",
            ),
        ),
        execution_policy=ResolvedExecutionPolicy(approval="auto", sandbox="workspace-write"),
        extra_args=("--permission-mode", "acceptEdits"),
    )

    spawn_id = str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="claude-sonnet-4-6",
            agent="coder",
            harness="claude",
            prompt="hello",
            launch_policy_snapshot=snapshot,
        )
    )

    spawn_dir = runtime_root / "spawns" / spawn_id
    state_path = spawn_dir / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert payload["launch_policy_snapshot"] == snapshot.model_dump(mode="json")
    assert not any(
        "launch_policy_snapshot" in path.name
        for path in spawn_dir.iterdir()
        if path.name != "state.json"
    )


def test_spawn_state_without_launch_policy_snapshot_remains_readable(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
        )
    )
    paths = RuntimePaths.from_root_dir(runtime_root)
    state_path = paths.spawns_dir / spawn_id / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload.pop("launch_policy_snapshot", None)
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    row = read_state(paths.spawns_dir, spawn_id)
    assert row is not None
    assert row.launch_policy_snapshot is None


def test_record_cancel_intent_persists_first_spawn_level_request(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    first = record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )
    second = record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=143,
        error="terminated",
        requested_at="2026-06-03T01:01:00Z",
    )

    assert first is not None
    assert first.cancel_intent is not None
    assert first.cancel_intent.exit_code == 130
    assert second is not None
    assert second.cancel_intent == first.cancel_intent
    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.cancel_intent == first.cancel_intent


def test_record_cancel_intent_leaves_terminal_spawn_unchanged(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)
    finalize_spawn(runtime_root, spawn_id, "succeeded", 0, origin="runner")

    result = record_cancel_intent(
        runtime_root,
        spawn_id,
        exit_code=130,
        error="cancelled",
        requested_at="2026-06-03T01:00:00Z",
    )

    assert result is not None
    assert result.status == "succeeded"
    assert result.cancel_intent is None


def test_start_spawn_rejects_empty_goal(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)

    with pytest.raises(ValueError, match="--goal cannot be empty"):
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            goal="   ",
        )

    assert list_spawns(runtime_root) == []


def test_list_spawns_filters_v2_rows_and_keeps_listings_promptless(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    p1 = _start_test_spawn(runtime_root)
    p2 = str(
        start_spawn(
            runtime_root,
            chat_id="c2",
            model="gpt-5.4",
            agent="reviewer",
            harness="codex",
            prompt="world",
            desc="desc-2",
        )
    )
    finalize_spawn(runtime_root, p1, status="succeeded", exit_code=0, origin="runner")

    filtered = list_spawns(runtime_root, filters={"status": "running", "agent": "reviewer"})

    assert [spawn.id for spawn in filtered] == [p2]
    assert filtered[0].prompt is None
    assert filtered[0].desc == "desc-2"


def test_spawn_queries_read_v2_state_and_prompt(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    p1 = _start_test_spawn(runtime_root)
    p2 = str(
        start_spawn(
            runtime_root,
            chat_id="c2",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="world",
        )
    )
    finalize_spawn(runtime_root, p1, status="succeeded", exit_code=0, origin="runner")

    spawns = list_spawns(runtime_root)
    assert [spawn.id for spawn in spawns] == [p1, p2]
    assert spawns[0].status == "succeeded"
    assert spawns[1].status == "running"
    assert all(spawn.prompt is None for spawn in spawns)

    row = get_spawn(runtime_root, p2)
    assert row is not None
    assert row.id == p2
    assert row.chat_id == "c2"
    assert row.status == "running"
    assert row.prompt == "world"


def test_next_and_reserved_spawn_ids_ignore_opaque_dirs_and_honor_highest_seed(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)
    spawns_dir = runtime_root / "spawns"
    spawns_dir.mkdir(parents=True, exist_ok=True)
    for spawn_id in ("abc", "p7x", "p5", "p12"):
        spawn_dir = spawns_dir / spawn_id
        spawn_dir.mkdir(parents=True, exist_ok=True)
        if spawn_id.startswith("p") and spawn_id[1:].isdigit():
            state_payload = f'{{"v":2,"revision":1,"id":"{spawn_id}"}}\n'
            (spawn_dir / "state.json").write_text(state_payload, encoding="utf-8")
    (runtime_root / "spawn-id-counter").write_text("20\n", encoding="utf-8")

    assert next_spawn_id(runtime_root) == "p21"
    assert reserve_spawn_id(runtime_root) == "p21"
    assert next_spawn_id(runtime_root) == "p22"
    assert (runtime_root / "spawn-id-counter").read_text(encoding="utf-8").strip() == "21"


def test_exited_event_is_non_terminal_and_projects_last_attempt_exit(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    record_spawn_exited(
        runtime_root,
        spawn_id,
        exit_code=143,
        exited_at="2026-04-12T14:00:00Z",
    )

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "running"
    assert row.last_attempt_exited_at == "2026-04-12T14:00:00Z"
    assert row.last_attempt_exit_code == 143
    assert row.exit_code is None


def test_record_runner_exit_persists_terminal_intent_without_finalizing(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)

    record_runner_exit(
        runtime_root,
        spawn_id,
        status="failed",
        exit_code=42,
        error="guardrail_failed",
        exited_at="2026-04-12T14:03:00Z",
    )

    row = get_spawn(runtime_root, spawn_id)
    assert row is not None
    assert row.status == "running"
    assert row.runner_exit_status == "failed"
    assert row.runner_exit_code == 42
    assert row.runner_exit_error == "guardrail_failed"
    assert row.runner_exit_at == "2026-04-12T14:03:00Z"
