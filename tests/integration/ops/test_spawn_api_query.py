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
    SpawnListInput,
    SpawnShowInput,
    SpawnStatsInput,
    SpawnWaitInput,
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


def test_spawn_list_does_not_infer_running_star_from_exited_at(tmp_path: Path) -> None:
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


def test_spawn_list_and_show_suppress_terminal_primary_activity(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    spawn_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="done",
        status="succeeded",
    )
    _write_primary_meta(
        runtime_root,
        str(spawn_id),
        activity="finalizing",
        backend_pid=4242,
        tui_pid=4343,
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

    detail = spawn_api.spawn_show_sync(
        SpawnShowInput(project_root=project_root.as_posix(), spawn_id="p42")
    )
    assert detail.status == "succeeded"
    assert detail.activity is None
    assert detail.backend_pid == 4242
    assert detail.tui_pid == 4343


def test_spawn_show_includes_persisted_goal_text_and_json(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)

    spawn_id = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p77",
        chat_id="c77",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="hello",
        goal="ship the migration",
    )

    detail = spawn_api.spawn_show_sync(
        SpawnShowInput(project_root=project_root.as_posix(), spawn_id=str(spawn_id))
    )

    assert detail.goal == "ship the migration"
    rendered = detail.format_text()
    assert "Goal: ship the migration" in rendered
    wire = detail.to_cli_wire()
    assert wire["goal"] == "ship the migration"


def test_wait_yield_default_uses_parent_harness_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "\n".join(
            [
                "[spawn]",
                "default_wait_yield_seconds = 240",
                "min_wait_yield_seconds = 30",
                "",
                "[harness.claude]",
                "wait_yield_seconds = 270",
                "",
                "[harness.codex]",
                "wait_yield_seconds = 900",
            ]
        ),
        encoding="utf-8",
    )
    config = spawn_api.load_config(project_root)

    monkeypatch.setenv("MERIDIAN_HARNESS", "claude")
    assert (
        spawn_api._resolve_wait_checkpoint_seconds(
            payload=SpawnWaitInput(),
            spawn_ids=("p-claude-child", "p-codex-child"),
            project_root=project_root,
            config=config,
        )
        == 270.0
    )

    monkeypatch.setenv("MERIDIAN_HARNESS", "codex")
    assert (
        spawn_api._resolve_wait_checkpoint_seconds(
            payload=SpawnWaitInput(),
            spawn_ids=("p-claude-child", "p-unknown-child"),
            project_root=project_root,
            config=config,
        )
        == 900.0
    )

    monkeypatch.delenv("MERIDIAN_HARNESS", raising=False)
    assert (
        spawn_api._resolve_wait_checkpoint_seconds(
            payload=SpawnWaitInput(),
            spawn_ids=("p-codex-child",),
            project_root=project_root,
            config=config,
        )
        == 240.0
    )


def test_wait_yield_override_wins_over_harness_defaults(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    spawn_id = spawn_store.start_spawn(
        runtime_root,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="codex",
    )
    config = spawn_api.load_config(project_root)

    assert (
        spawn_api._resolve_wait_checkpoint_seconds(
            payload=SpawnWaitInput(yield_after_secs=12),
            spawn_ids=(str(spawn_id),),
            project_root=project_root,
            config=config,
        )
        == 12
    )
