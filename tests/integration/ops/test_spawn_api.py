import json
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.launch.constants import PRIMARY_META_FILENAME
from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnCancelAllInput,
    SpawnCancelInput,
    SpawnCreateInput,
    SpawnListInput,
    SpawnShowInput,
    SpawnStatsInput,
    SpawnWaitInput,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.telemetry import init_telemetry
from meridian.lib.telemetry.events import TelemetryEnvelope


class RecordingTelemetrySink:
    def __init__(self) -> None:
        self.events: list[TelemetryEnvelope] = []

    def write_batch(self, events: Sequence[TelemetryEnvelope]) -> None:
        self.events.extend(events)

    def close(self) -> None:
        pass


def wait_for_telemetry(predicate: object, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.01)
    raise AssertionError("telemetry event not observed")


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")


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


def _noop_setup_telemetry(**_kwargs: object) -> None:
    pass


def test_spawn_create_dry_run_resolves_project_root_from_nested_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    nested = project_root / "src" / "feature"
    (project_root / ".mars" / "skills").mkdir(parents=True)
    nested.mkdir(parents=True)
    reference_file = project_root / "guide.md"
    reference_file.write_text("# Guide\n", encoding="utf-8")
    monkeypatch.chdir(nested)

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="",
            files=("guide.md",),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert result.project_root == project_root.resolve().as_posix()
    assert result.project_root_source == "mars"
    assert result.runtime_root is None
    assert result.runtime_root_source == "unresolved"
    resolved_reference = reference_file.resolve()
    assert len(result.reference_files) == 1
    assert Path(result.reference_files[0]).resolve() == resolved_reference
    composed_prompt = result.composed_prompt or ""
    assert (
        str(resolved_reference) in composed_prompt
        or resolved_reference.as_posix() in composed_prompt
    )


def test_spawn_create_dry_run_emits_usage_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    monkeypatch.setattr(spawn_api, "setup_telemetry", _noop_setup_telemetry)
    sink = RecordingTelemetrySink()
    init_telemetry(sink=sink)

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.3-codex",
            harness="codex",
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    wait_for_telemetry(
        lambda: {"usage.model.selected", "usage.spawn.launched"}.issubset(
            {event.event for event in sink.events}
        )
    )
    usage_events = {event.event: event for event in sink.events if event.domain == "usage"}
    assert usage_events["usage.model.selected"].data == {
        "model_family": "gpt-5.3",
        "harness": "codex",
    }
    assert "gpt-5.3-codex" not in json.dumps(usage_events["usage.model.selected"].to_dict())
    assert usage_events["usage.spawn.launched"].data == {"harness": "codex"}


def test_spawn_create_with_prepared_skips_self_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    prepared = prepare_for_runtime_write(project_root)

    def _forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("self-bootstrap helper should not be called")

    monkeypatch.setattr(spawn_api, "setup_telemetry", _forbidden)
    monkeypatch.setattr(spawn_api, "load_config", _forbidden)
    monkeypatch.setattr(spawn_api, "resolve_runtime_root_and_config", _forbidden)
    monkeypatch.setattr(spawn_api, "resolve_runtime_root", _forbidden)

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.3-codex",
            harness="codex",
            project_root=project_root.as_posix(),
            dry_run=True,
        ),
        prepared=prepared,
    )

    assert result.status == "dry-run"
    assert result.harness_id == "codex"


def test_spawn_create_dry_run_surfaces_goal_and_contract_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    monkeypatch.setattr(
        spawn_api,
        "build_create_payload",
        lambda payload, runtime=None, preflight_warning=None, ctx=None: type(
            "Prepared",
            (),
            {
                "harness": payload.harness or "codex",
                "model": payload.model,
                "warning": preflight_warning,
                "agent": payload.agent,
                "agent_metadata": {},
                "skills": payload.skills,
                "skill_paths": (),
                "reference_files": (),
                "template_vars": {},
                "context_from": (),
                "prompt": payload.prompt,
                "goal": payload.goal,
                "model_selection_requested_token": None,
                "model_selection_canonical_id": None,
                "model_selection_harness_provenance": None,
                "terminal_surface_mode": None,
                "cli_command": ("codex",),
            },
        )(),
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            goal="ship phase 3",
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert result.goal == "ship phase 3"
    goal_contract_preview = result.goal_contract_preview
    assert goal_contract_preview is not None
    assert "# Spawn Goal" in goal_contract_preview
    assert "ship phase 3" in goal_contract_preview


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

    output = spawn_api.spawn_stats_sync(
        SpawnStatsInput(project_root=project_root.as_posix())
    )

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


def test_spawn_list_does_not_infer_running_star_from_exited_at(
    tmp_path: Path,
) -> None:
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


def test_spawn_list_and_show_suppress_terminal_primary_activity(
    tmp_path: Path,
) -> None:
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
