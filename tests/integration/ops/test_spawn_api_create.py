"""spawn_create_sync behavior — dry-run resolution, telemetry, and goal preview.

Query/list/cancel/wait tests live in test_spawn_api_query.py.

# qa-validated: test-suite-redesign
"""

import json
import subprocess
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

import meridian.lib.ops.spawn.api as spawn_api
import meridian.lib.ops.spawn.pre_init as pre_init_module
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.types import HarnessId
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.state import spawn_store, work_repository, work_store
from meridian.lib.state.paths import resolve_project_paths, resolve_spawn_log_dir
from meridian.lib.telemetry import init_telemetry
from tests.support.fakes import RecordingTelemetrySink, wait_for_telemetry
from tests.support.launch import FakeBundleResult, stub_bundle_request_and_resolve


def _noop_setup_telemetry(**_kwargs: object) -> None:
    pass


def test_background_headless_deny_rejects_before_reservation_without_affecting_allowed_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    (project_root / "meridian.toml").write_text(
        '[spawn]\ndeny_headless_harnesses = ["codex"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    prepared = prepare_for_runtime_write(project_root)
    worker_launches: list[tuple[object, ...]] = []
    real_popen = subprocess.Popen

    def _resolve_bundle(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> FakeBundleResult:
        _ = harness_registry
        harness = HarnessId(request.harness_override or "codex")
        model = request.model_override or "test-model"
        return FakeBundleResult(
            model=model,
            model_token=model,
            harness=harness,
            harness_model=model,
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "cli"},
        )

    class _FakePopen:
        pid = 42424

    def _launch_worker(*args: object, **kwargs: object) -> object:
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, tuple) and "meridian.lib.ops.spawn.execute_bg" in command:
            worker_launches.append((*args, kwargs))
            return _FakePopen()
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _resolve_bundle)
    monkeypatch.setattr(subprocess, "Popen", _launch_worker)

    denied = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="denied",
            model="gpt-5.4",
            harness="codex",
            project_root=project_root.as_posix(),
            background=True,
        ),
        prepared=prepared,
    )
    allowed = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="allowed",
            model="gemini-2.5-pro",
            harness="opencode",
            project_root=project_root.as_posix(),
            background=True,
        ),
        prepared=prepared,
    )

    assert denied.status == "failed"
    assert denied.spawn_id is None
    assert denied.message is not None
    assert "Headless spawns on the 'codex' harness are denied" in denied.message
    assert allowed.status == "running"
    assert allowed.spawn_id is not None
    assert len(worker_launches) == 1
    assert prepared.runtime_root is not None
    rows = spawn_store.list_spawns(prepared.runtime_root).records
    assert [row.id for row in rows] == [allowed.spawn_id]
    spawns_dir = prepared.runtime_root / "spawns"
    assert [
        path.name
        for path in spawns_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ] == [allowed.spawn_id]
    assert list((spawns_dir / ".staging").iterdir()) == []

    allowed_log_dir = resolve_spawn_log_dir(
        project_root,
        allowed.spawn_id,
        runtime_root=prepared.runtime_root,
    )
    params = json.loads((allowed_log_dir / "params.json").read_text(encoding="utf-8"))
    assert params["phase"] == "request"
    assert params["model"] == "gemini-2.5-pro"
    assert params["harness"] == "opencode"
    assert params["prompt_length"] == len("allowed")

    worker_request = json.loads(
        (allowed_log_dir / "bg-worker-request.json").read_text(encoding="utf-8")
    )
    assert worker_request["request"]["prompt"] == "allowed"
    assert worker_request["request"]["harness"] == "opencode"

    launch_command = worker_launches[0][0]
    assert isinstance(launch_command, tuple)
    assert launch_command[launch_command.index("--spawn-id") + 1] == allowed.spawn_id


def test_spawn_create_dry_run_resolves_project_root_from_explicit_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    nested = project_root / "src" / "feature"
    (project_root / ".mars" / "skills").mkdir(parents=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    nested.mkdir(parents=True)
    reference_file = project_root / "guide.md"
    reference_file.write_text("# Guide\n", encoding="utf-8")
    monkeypatch.chdir(nested)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4-mini",
            files=("guide.md",),
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert result.project_root == project_root.resolve().as_posix()
    assert result.project_root_source == "explicit"
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
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX,
    )
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
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        harness=HarnessId.CODEX,
    )

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


def test_spawn_create_build_payload_failure_returns_pre_init_failed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    prepared = prepare_for_runtime_write(project_root)

    def _raise_build_create_payload(*_args: object, **_kwargs: object) -> object:
        raise ValueError("model resolution failed")

    monkeypatch.setattr(spawn_api, "build_create_payload", _raise_build_create_payload)

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4",
            harness="opencode",
            agent="browser-prober",
            project_root=project_root.as_posix(),
            task_dir=(tmp_path / "task").as_posix(),
            work="browser-investigation",
        ),
        prepared=prepared,
    )

    assert result.status == "failed"
    assert result.spawn_id is None
    assert result.error == "pre_init_failed"
    assert result.exit_code == 1
    assert result.model == "gpt-5.4"
    assert result.harness_id == "opencode"
    assert result.message is not None
    assert "model resolution failed" in result.message
    assert result.to_wire()["message"] == result.message
    assert result.to_wire()["model"] == "gpt-5.4"
    assert result.to_wire()["harness_id"] == "opencode"
    assert result.to_wire()["task_cwd_source"] == "explicit-task-dir"
    assert result.to_wire()["task_cwd_work_item"] == "browser-investigation"


def test_spawn_create_unexpected_pre_init_exception_logs_traceback_and_distinct_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    prepared = prepare_for_runtime_write(project_root)

    def _raise_build_create_payload(*_args: object, **_kwargs: object) -> object:
        raise TypeError("bug in launch composition")

    monkeypatch.setattr(spawn_api, "build_create_payload", _raise_build_create_payload)

    saved_logger = pre_init_module.logger
    with capture_logs() as logs:
        pre_init_module.logger = structlog.get_logger(pre_init_module.__name__)
        try:
            result = spawn_api.spawn_create_sync(
                SpawnCreateInput(
                    prompt="run",
                    model="gpt-5.4",
                    harness="opencode",
                    project_root=project_root.as_posix(),
                ),
                prepared=prepared,
            )
        finally:
            pre_init_module.logger = saved_logger

    assert result.status == "failed"
    assert result.error == "pre_init_unexpected_error"
    assert result.message is not None
    assert "TypeError: bug in launch composition" in result.message
    failure_log = next(
        (log for log in logs if log["event"] == "spawn_pre_init_unexpected_exception"),
        None,
    )
    assert failure_log is not None
    assert failure_log["error_type"] == "TypeError"
    assert failure_log["exc_info"] is True


def test_spawn_create_validation_failure_returns_pre_init_failed_output(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    prepared = prepare_for_runtime_write(project_root)

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="",
            model="gpt-5.4",
            harness="opencode",
            project_root=project_root.as_posix(),
            task_dir=(tmp_path / "task").as_posix(),
            work="browser-investigation",
        ),
        prepared=prepared,
    )

    assert result.status == "failed"
    assert result.spawn_id is None
    assert result.error == "pre_init_failed"
    assert result.exit_code == 1
    assert result.model == "gpt-5.4"
    assert result.harness_id == "opencode"
    assert result.message is not None
    assert "prompt required" in result.message
    assert result.to_wire()["message"] == result.message
    assert result.to_wire()["model"] == "gpt-5.4"
    assert result.to_wire()["harness_id"] == "opencode"
    assert result.to_wire()["task_cwd_source"] == "explicit-task-dir"
    assert result.to_wire()["task_cwd_work_item"] == "browser-investigation"


def test_spawn_create_dry_run_with_work_is_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )
    project_state_dir = resolve_project_paths(project_root).root_dir

    assert work_store.get_work_item(project_state_dir, "new-work-item") is None

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4-mini",
            work="new-work-item",
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert work_store.get_work_item(project_state_dir, "new-work-item") is None
    assert result.task_cwd_source == "explicit-work-authority-root"
    assert result.task_cwd == project_root.as_posix()
    assert result.reference_anchor == project_root.as_posix()
    assert result.task_cwd_work_item == "new-work-item"
    assert result.warning is not None
    assert "would be created on launch" in result.warning


def test_spawn_create_dry_run_uses_explicit_task_dir_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )
    task_dir = tmp_path / "task-override"
    task_dir.mkdir(parents=True, exist_ok=True)

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4-mini",
            task_dir=task_dir.as_posix(),
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert result.task_cwd_source == "explicit-task-dir"
    assert result.task_cwd == task_dir.as_posix()
    assert result.reference_anchor == task_dir.as_posix()


def test_spawn_create_dry_run_uses_ambient_work_item_task_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )
    project_state_dir = resolve_project_paths(project_root).root_dir
    work = work_repository.create_work_item(project_state_dir, "ambient-task-dir", "", None)
    ambient_task_dir = tmp_path / "ambient-task-dir"
    ambient_task_dir.mkdir(parents=True, exist_ok=True)
    work_repository.update_work_item_task_dir(
        project_state_dir,
        work.name,
        task_dir=ambient_task_dir.as_posix(),
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4-mini",
            project_root=project_root.as_posix(),
            dry_run=True,
        ),
        ctx=RuntimeContext(work_id=work.name),
    )

    assert result.status == "dry-run"
    assert result.task_cwd_source == "ambient-work-task-dir"
    assert result.task_cwd == ambient_task_dir.as_posix()
    assert result.reference_anchor == ambient_task_dir.as_posix()
    assert result.task_cwd_work_item == work.name
