# qa-validated: test-suite-redesign

"""CRUD operation tests for the spawn store.

Covers: start, get, list, update, next_id, reserve, mark_running, and
attempt-exit and runner-exit metadata writes. Finalization and concurrency live in
test_spawn_store_finalize.py.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from meridian.lib.bootstrap.runtime_state import ensure_runtime_dirs
from meridian.lib.core.domain import SkillContent
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.spawn_start import SpawnStartMetadata
from meridian.lib.state import spawn_store as spawn_store_module
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.spawn.repository import (
    SpawnStateQuarantined,
    read_state,
    scan_spawn_ids,
)
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
from tests.conftest import posix_only


def _state_root(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".meridian"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _start_test_spawn(runtime_root: Path, *, spawn_id: str | None = None) -> str:
    return str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            spawn_id=spawn_id,
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


def test_invalid_persisted_status_is_reported_and_not_coerced(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)
    valid_spawn_id = _start_test_spawn(runtime_root)
    state_path = RuntimePaths.from_root_dir(runtime_root).spawns_dir / spawn_id / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["status"] = "zombie"
    atomic_write_text(state_path, json.dumps(payload))

    with pytest.raises(SpawnStateQuarantined) as single:
        get_spawn(runtime_root, spawn_id)
    collection = list_spawns(runtime_root)

    assert [row.id for row in collection] == [valid_spawn_id]
    assert len(collection.quarantines) == 1
    assert single.value.report.spawn_id == collection.quarantines[0].spawn_id == spawn_id
    assert single.value.report.state_path == collection.quarantines[0].state_path
    assert "zombie" in str(single.value.report.validation_errors)
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "zombie"


def test_invalid_persisted_kind_is_reported_and_not_coerced(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)
    state_path = RuntimePaths.from_root_dir(runtime_root).spawns_dir / spawn_id / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["kind"] = "worker"
    atomic_write_text(state_path, json.dumps(payload))

    with pytest.raises(SpawnStateQuarantined):
        get_spawn(runtime_root, spawn_id)
    collection = list_spawns(runtime_root)

    assert collection == []
    assert [report.spawn_id for report in collection.quarantines] == [spawn_id]
    assert json.loads(state_path.read_text(encoding="utf-8"))["kind"] == "worker"


def test_persisted_spawn_identities_are_normalized_at_parse(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)
    state_path = RuntimePaths.from_root_dir(runtime_root).spawns_dir / spawn_id / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload.update(
        chat_id="  c1  ",
        owner_chat_id="   ",
        harness_session_id="  thread-1  ",
    )
    atomic_write_text(state_path, json.dumps(payload))

    row = get_spawn(runtime_root, spawn_id)

    assert row is not None
    assert row.chat_id == "c1"
    assert row.owner_chat_id is None
    assert row.harness_session_id == "thread-1"


@pytest.mark.parametrize("invalid_status", [[], {}, 123])
def test_non_string_persisted_status_is_quarantined(
    tmp_path: Path,
    invalid_status: object,
) -> None:
    runtime_root = _state_root(tmp_path)
    spawn_id = _start_test_spawn(runtime_root)
    state_path = RuntimePaths.from_root_dir(runtime_root).spawns_dir / spawn_id / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["status"] = invalid_status
    atomic_write_text(state_path, json.dumps(payload))

    with pytest.raises(SpawnStateQuarantined) as quarantined:
        get_spawn(runtime_root, spawn_id)

    assert quarantined.value.report.spawn_id == spawn_id
    assert str(invalid_status) in str(quarantined.value.report.validation_errors)


def test_start_spawn_publishes_only_a_complete_readable_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    spawn_id = "p7"
    publication_build_started = threading.Event()
    release_publication_build = threading.Event()
    created_spawn_ids: list[str] = []
    errors: list[BaseException] = []
    staged_dirs: list[Path] = []
    original_atomic_publish_dir = spawn_store_module.atomic_publish_dir

    def pause_before_publish(stage_dir: Path, dest_dir: Path) -> None:
        staged_dirs.append(stage_dir)
        publication_build_started.set()
        if not release_publication_build.wait(timeout=5):
            raise TimeoutError("publication build was not released")
        original_atomic_publish_dir(stage_dir, dest_dir)

    monkeypatch.setattr(spawn_store_module, "atomic_publish_dir", pause_before_publish)

    def create_spawn() -> None:
        try:
            created_spawn_ids.append(
                str(
                    start_spawn(
                        runtime_root,
                        chat_id="c1",
                        model="gpt-5.4",
                        agent="coder",
                        harness="codex",
                        prompt="hello",
                        spawn_id=spawn_id,
                    )
                )
            )
        except BaseException as exc:
            errors.append(exc)

    publisher = threading.Thread(target=create_spawn)
    publisher.start()

    try:
        assert publication_build_started.wait(timeout=5)
        assert scan_spawn_ids(paths.spawns_dir) == []
        assert not (paths.spawns_dir / spawn_id).exists()
        assert len(staged_dirs) == 1
        stage_dir = staged_dirs[0]
        assert stage_dir.parent == paths.spawns_dir / ".staging"
        assert (stage_dir / "starting-prompt.md").read_text(encoding="utf-8") == "hello"
        staged_row = read_state(stage_dir.parent, stage_dir.name)
        assert staged_row is not None
        assert staged_row.id == spawn_id
        assert staged_row.prompt == "hello"
    finally:
        release_publication_build.set()
        publisher.join(timeout=5)

    assert not publisher.is_alive()
    assert errors == []
    assert created_spawn_ids == [spawn_id]
    assert scan_spawn_ids(paths.spawns_dir) == [spawn_id]
    assert (paths.spawns_dir / spawn_id / "starting-prompt.md").read_text(
        encoding="utf-8"
    ) == "hello"
    row = read_state(paths.spawns_dir, spawn_id)
    assert row is not None
    assert row.id == spawn_id
    assert row.prompt == "hello"


def test_runtime_startup_removes_only_abandoned_stages(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    final_spawn_dir = paths.spawns_dir / "p1"
    final_spawn_dir.mkdir(parents=True)
    (final_spawn_dir / "state.json").write_text("published\n", encoding="utf-8")
    abandoned_stage = paths.spawns_dir / ".staging" / "p2-1234-deadbeef"
    abandoned_stage.mkdir(parents=True)
    (abandoned_stage / "state.json").write_text("abandoned\n", encoding="utf-8")
    abandoned_file = paths.spawns_dir / ".staging" / "orphan.tmp"
    abandoned_file.write_text("abandoned\n", encoding="utf-8")

    ensure_runtime_dirs(runtime_root)

    assert (final_spawn_dir / "state.json").read_text(encoding="utf-8") == "published\n"
    assert list((paths.spawns_dir / ".staging").iterdir()) == []


@posix_only
def test_runtime_startup_does_not_follow_symlinked_staging_container(
    tmp_path: Path,
) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    paths.spawns_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    victim = outside_dir / "keep.txt"
    victim.write_text("keep\n", encoding="utf-8")
    staging_dir = paths.spawns_dir / ".staging"
    staging_dir.symlink_to(outside_dir, target_is_directory=True)

    ensure_runtime_dirs(runtime_root)

    assert victim.read_text(encoding="utf-8") == "keep\n"
    assert not staging_dir.is_symlink()


@posix_only
def test_start_spawn_rejects_symlinked_staging_container(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    paths = RuntimePaths.from_root_dir(runtime_root)
    paths.spawns_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    victim = outside_dir / "keep.txt"
    victim.write_text("keep\n", encoding="utf-8")
    (paths.spawns_dir / ".staging").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(NotADirectoryError, match="staging container must be a real directory"):
        _start_test_spawn(runtime_root, spawn_id="p1")

    assert victim.read_text(encoding="utf-8") == "keep\n"
    assert not (paths.spawns_dir / "p1").exists()


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


def test_record_cancel_intent_persists_first_request_and_skips_terminal(
    tmp_path: Path,
) -> None:
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

    terminal_spawn_id = _start_test_spawn(runtime_root, spawn_id="p-terminal")
    finalize_spawn(runtime_root, terminal_spawn_id, "succeeded", 0, origin="runner")

    result = record_cancel_intent(
        runtime_root,
        terminal_spawn_id,
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


def test_list_spawns_reports_schema_invalid_row(tmp_path: Path) -> None:
    runtime_root = _state_root(tmp_path)
    _start_test_spawn(runtime_root)
    invalid_spawn_dir = runtime_root / "spawns" / "p999"
    invalid_spawn_dir.mkdir(parents=True)
    atomic_write_text(invalid_spawn_dir / "state.json", json.dumps({"id": "p999"}))

    collection = list_spawns(runtime_root)
    assert [row.id for row in collection] == ["p1"]
    assert [report.spawn_id for report in collection.quarantines] == ["p999"]


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
            state_payload = f'{{"v":2,"id":"{spawn_id}"}}\n'
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
