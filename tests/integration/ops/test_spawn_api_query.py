# qa-validated: pi-rpc-quiescence
"""spawn stats, list, show, cancel_all, and wait checkpoint behaviors.

spawn_create_sync tests live in test_spawn_api_create.py.

# qa-validated: test-suite-redesign
"""

import json
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.launch.constants import PRIMARY_META_FILENAME
from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnCancelAllInput,
    SpawnCancelInput,
    SpawnChildrenInput,
    SpawnListInput,
    SpawnShowInput,
    SpawnStatsInput,
    SpawnStatusInput,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


def _state_root(project_root: Path) -> Path:
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _write_primary_meta(
    runtime_root: Path,
    spawn_id: str,
    *,
    activity: str,
    backend_pid: int | None = None,
    tui_pid: int | None = None,
) -> None:
    meta_path = runtime_root / "spawns" / spawn_id / PRIMARY_META_FILENAME
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "managed_backend": True,
                "activity": activity,
                "backend_pid": backend_pid,
                "tui_pid": tui_pid,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_spawn_stats_includes_finalizing_bucket(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    running_id = spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="running",
    )
    finalizing_id = spawn_store.start_spawn(
        runtime_root,
        chat_id="c2",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="finalizing",
    )
    assert spawn_store.mark_finalizing(runtime_root, finalizing_id) is True
    succeeded_id = spawn_store.start_spawn(
        runtime_root,
        chat_id="c3",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="done",
    )
    spawn_store.finalize_spawn(
        runtime_root,
        succeeded_id,
        status="succeeded",
        exit_code=0,
        origin="runner",
    )

    output = spawn_api.spawn_stats_sync(SpawnStatsInput(project_root=project_root.as_posix()))

    assert output.total_runs == 3
    assert output.running == 1
    assert output.finalizing == 1
    assert output.succeeded == 1
    model_stats = output.models["gpt-5.4"]
    assert model_stats.running == 1
    assert model_stats.finalizing == 1
    assert running_id != finalizing_id


def test_spawn_stats_session_filters_by_exact_chat_id(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-owner-child",
        chat_id="c-child",
        owner_chat_id="c-owner",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="child",
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-sibling",
        chat_id="c-sibling",
        owner_chat_id="c-owner",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="sibling",
    )

    child_only = spawn_api.spawn_stats_sync(
        SpawnStatsInput(project_root=project_root.as_posix(), session="c-child")
    )
    owner_family = spawn_api.spawn_stats_sync(
        SpawnStatsInput(project_root=project_root.as_posix(), session="c-owner")
    )

    assert child_only.total_runs == 1
    assert owner_family.total_runs == 0


def test_spawn_cancel_all_counts_finalizing_cancellations_as_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    runtime_root = prepared.runtime_root
    assert runtime_root is not None

    spawn_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p11",
        chat_id="c11",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="running",
    )

    def _fake_spawn_cancel_sync(
        _payload: SpawnCancelInput,
        ctx=None,
        *,
        sink=None,
        prepared=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink, prepared)
        return SpawnActionOutput(
            command="spawn.cancel",
            status="finalizing",
            spawn_id=str(spawn_id),
            message="Spawn did not terminate within grace; reaper will reconcile.",
        )

    monkeypatch.setattr(spawn_api, "spawn_cancel_sync", _fake_spawn_cancel_sync)

    output = spawn_api.spawn_cancel_all_sync(
        SpawnCancelAllInput(project_root=project_root.as_posix(), include_others=True),
        prepared=prepared,
    )

    assert output.total_running == 1
    assert output.cancelled_count == 1
    assert output.finalizing_count == 1
    assert output.failed_count == 0
    assert (
        output.format_text()
        == "Requested cancellation for 1 running spawn(s).\n1 cancellation(s) still finalizing."
    )


def test_spawn_list_does_not_infer_running_star_from_last_attempt_exit(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    spawn_id = spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="hello",
    )
    spawn_store.record_spawn_exited(
        runtime_root,
        spawn_id,
        exit_code=143,
        exited_at="2026-04-13T10:00:00Z",
    )

    output = spawn_api.spawn_list_sync(
        SpawnListInput(project_root=project_root.as_posix(), statuses=("running",))
    )

    assert len(output.spawns) == 1
    assert output.spawns[0].status == "running"
    assert output.spawns[0].status_display is None


def test_spawn_list_filters_by_profile_name(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    reviewer_spawn_id = spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.4",
        agent="reviewer",
        harness="codex",
        prompt="review",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="c2",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="code",
    )

    output = spawn_api.spawn_list_sync(
        SpawnListInput(
            project_root=project_root.as_posix(),
            statuses=(),
            profile="reviewer",
        )
    )

    assert [entry.spawn_id for entry in output.spawns] == [str(reviewer_spawn_id)]


def test_spawn_list_and_status_surface_timed_out_status(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    timed_out_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-timeout",
        chat_id="c-timeout",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="timeout",
    )
    spawn_store.finalize_spawn(
        runtime_root,
        timed_out_id,
        "timed_out",
        1,
        origin="runner",
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-running",
        chat_id="c-running",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="running",
    )

    listed = spawn_api.spawn_list_sync(
        SpawnListInput(project_root=project_root.as_posix(), statuses=("timed_out",))
    )
    status = spawn_api.spawn_status_sync(
        SpawnStatusInput(project_root=project_root.as_posix(), spawn_id=str(timed_out_id))
    )

    assert [entry.spawn_id for entry in listed.spawns] == [str(timed_out_id)]
    assert listed.spawns[0].status == "timed_out"
    assert status.status == "timed_out"
    assert status.exit_code == 1


def test_spawn_status_omits_report_body_until_requested(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    spawn_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-report-toggle",
        chat_id="c-report-toggle",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="hello",
    )
    report_path = runtime_root / "spawns" / str(spawn_id) / "report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("report body\n", encoding="utf-8")
    spawn_store.finalize_spawn(
        runtime_root,
        spawn_id,
        "succeeded",
        0,
        origin="runner",
    )

    status_detail = spawn_api.spawn_status_sync(
        SpawnStatusInput(project_root=project_root.as_posix(), spawn_id=str(spawn_id))
    )
    reported_status_detail = spawn_api.spawn_status_sync(
        SpawnStatusInput(
            project_root=project_root.as_posix(),
            spawn_id=str(spawn_id),
            include_report_body=True,
        )
    )
    show_detail = spawn_api.spawn_show_sync(
        SpawnShowInput(project_root=project_root.as_posix(), spawn_id=str(spawn_id))
    )

    assert status_detail.report_path == report_path.as_posix()
    assert status_detail.report_body is None
    assert reported_status_detail.report_body == "report body"
    assert show_detail.report_body == "report body"


def test_spawn_show_and_list_hydrate_primary_and_pi_diagnostics(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    primary_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="done",
    )
    spawn_store.finalize_spawn(runtime_root, primary_id, "succeeded", 0, origin="runner")
    _write_primary_meta(
        runtime_root,
        str(primary_id),
        activity="finalizing",
        backend_pid=4242,
        tui_pid=4343,
    )

    pi_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-cleanup",
        chat_id="c-cleanup",
        model="gpt-5.4",
        agent="coder",
        harness="pi",
        prompt="hello",
    )
    history_path = runtime_root / "spawns" / str(pi_id) / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "seq": 0,
                        "event_type": "meridian.pi.lifecycle.phase",
                        "payload": {"phase": "initial_prompt_sent"},
                    }
                ),
                json.dumps(
                    {
                        "seq": 1,
                        "event_type": "meridian.pi.lifecycle.phase",
                        "payload": {
                            "phase": "cleanup_stop_escalated",
                            "cleanup_status": "escalated",
                            "reason": "abort_grace_expired",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    listed = spawn_api.spawn_list_sync(
        SpawnListInput(project_root=project_root.as_posix(), statuses=("succeeded",))
    )
    assert len(listed.spawns) == 1
    entry = listed.spawns[0]
    assert entry.spawn_id == "p42"
    assert entry.status == "succeeded"
    assert entry.status_display is None
    assert entry.activity is None

    primary_detail = spawn_api.spawn_show_sync(
        SpawnShowInput(project_root=project_root.as_posix(), spawn_id="p42")
    )
    assert primary_detail.status == "succeeded"
    assert primary_detail.activity is None
    assert primary_detail.backend_pid == 4242
    assert primary_detail.tui_pid == 4343

    pi_detail = spawn_api.spawn_show_sync(
        SpawnShowInput(project_root=project_root.as_posix(), spawn_id=str(pi_id))
    )
    assert pi_detail.pi_lifecycle_phase == "cleanup_stop_escalated"
    assert pi_detail.pi_cleanup_status == "escalated"
    assert pi_detail.pi_cleanup_phase == "cleanup_stop_escalated"
    assert pi_detail.pi_cleanup_reason == "abort_grace_expired"


def test_spawn_children_uses_persisted_display_label_without_prompt_read(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    parent_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-parent",
        chat_id="c-parent",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="parent prompt",
        goal="parent goal",
    )
    child_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-child",
        parent_id=str(parent_id),
        chat_id="c-child",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="child prompt label",
        desc=None,
        goal=None,
    )

    listed = spawn_store.list_spawns(runtime_root, filters={"parent_id": str(parent_id)})
    assert len(listed) == 1
    child_row = listed[0]
    assert child_row.id == str(child_id)
    assert child_row.prompt is None
    assert child_row.display_label == "child prompt label"

    state_path = runtime_root / "spawns" / str(child_id) / "state.json"
    state_text = state_path.read_text(encoding="utf-8")
    assert '"display_label": "child prompt label"' in state_text

    output = spawn_api.spawn_children_sync(
        SpawnChildrenInput(project_root=project_root.as_posix(), spawn_id=str(parent_id))
    )

    assert len(output.spawns) == 1
    entry = output.spawns[0]
    assert entry.spawn_id == str(child_id)
    assert entry.desc == "child prompt label"


def test_spawn_children_prefers_goal_over_display_label(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    parent_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-parent",
        chat_id="c-parent",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="parent prompt",
    )
    child_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-child",
        parent_id=str(parent_id),
        chat_id="c-child",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="ignored prompt summary",
        goal="ship the feature",
    )

    listed = spawn_store.list_spawns(runtime_root, filters={"parent_id": str(parent_id)})
    assert listed[0].display_label is None

    output = spawn_api.spawn_children_sync(
        SpawnChildrenInput(project_root=project_root.as_posix(), spawn_id=str(parent_id))
    )

    assert output.spawns[0].spawn_id == str(child_id)
    assert output.spawns[0].desc == "ship the feature"


def test_spawn_children_legacy_row_without_display_label_returns_none_desc(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    parent_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-parent",
        chat_id="c-parent",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="parent prompt",
    )
    child_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-child",
        parent_id=str(parent_id),
        chat_id="c-child",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="legacy prompt body",
        desc=None,
        goal=None,
    )
    state_path = runtime_root / "spawns" / str(child_id) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("display_label", None)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    listed = spawn_store.list_spawns(runtime_root, filters={"parent_id": str(parent_id)})
    assert listed[0].prompt is None
    assert listed[0].display_label is None

    output = spawn_api.spawn_children_sync(
        SpawnChildrenInput(project_root=project_root.as_posix(), spawn_id=str(parent_id))
    )

    assert output.spawns[0].desc is None


def test_spawn_show_surfaces_persisted_goal_and_distinct_task_dir(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    task_cwd = tmp_path / "repo.worktrees" / "feature-x"
    task_cwd.mkdir(parents=True)

    spawn_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p-task-dir",
        chat_id="c-task-dir",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="hello",
        goal="ship the migration",
        work_id="feature-x",
        control_root=project_root.as_posix(),
        task_cwd=task_cwd.as_posix(),
    )

    detail = spawn_api.spawn_show_sync(
        SpawnShowInput(project_root=project_root.as_posix(), spawn_id=str(spawn_id))
    )

    assert detail.goal == "ship the migration"
    assert detail.task_cwd == task_cwd.as_posix()
