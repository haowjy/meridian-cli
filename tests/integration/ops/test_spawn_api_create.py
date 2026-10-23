"""spawn_create_sync behavior — dry-run resolution, telemetry, and goal preview.

Query/list/cancel/wait tests live in test_spawn_api_query.py.

# qa-validated: test-suite-redesign
"""

import json
import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

import meridian.lib.ops.spawn.api as spawn_api
import meridian.lib.ops.spawn.prepare as spawn_prepare
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.core.caller_boundary import (
    CALLER_BOUNDARY_ENV,
    CallerBoundary,
    CallerBoundaryCheck,
    current_pid_namespace,
)
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.ops.runtime import build_runtime
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.safety.policy import NetworkMode, PermissionPolicy, SandboxMode
from meridian.lib.state import spawn_store, work_repository, work_store
from meridian.lib.state.paths import resolve_project_paths, resolve_spawn_log_dir
from meridian.lib.telemetry import init_telemetry
from tests.support.fakes import RecordingTelemetrySink, wait_for_telemetry
from tests.support.launch import FakeBundleResult, stub_bundle_request_and_resolve


def _set_caller_guardrail(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sandbox: SandboxMode = "workspace-write",
    network: NetworkMode | None = None,
    has_spawn_capability: bool = True,
) -> None:
    record = json.loads(
        CallerBoundary(
            sandbox=sandbox,
            network_deny_floor=network == "deny",
            has_spawn_capability=has_spawn_capability,
            pid_namespace=None,
        ).to_env_value()
    )
    record["future_axis"] = "ignored"
    monkeypatch.setenv(
        CALLER_BOUNDARY_ENV,
        json.dumps(record),
    )


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
    )
    allowed = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="allowed",
            model="gemini-2.5-pro",
            harness="opencode",
            project_root=project_root.as_posix(),
            background=True,
        ),
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


def test_spawn_create_build_payload_failure_returns_pre_init_failed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")

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


def test_spawn_create_refuses_different_parent_pid_namespace_before_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = current_pid_namespace()
    if namespace is None:
        pytest.skip("PID namespaces are not exposed on this platform")
    monkeypatch.setenv(
        CALLER_BOUNDARY_ENV,
        CallerBoundary(
            sandbox="workspace-write",
            network_deny_floor=False,
            has_spawn_capability=True,
            pid_namespace=f"{namespace}:different",
        ).to_env_value(),
    )

    result = spawn_api.spawn_create_sync(SpawnCreateInput(prompt="run"))

    assert result.status == "failed"
    assert result.spawn_id is None
    assert result.error == "pre_init_failed"
    assert result.message is not None


def test_unsandboxed_caller_has_no_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        CALLER_BOUNDARY_ENV,
        json.dumps(
            {
                "pid_namespace": None,
                "sandbox": "default",
                # A newer axis and a missing spawn axis are irrelevant when unfloored.
                "future_axis": "ignored",
            }
        ),
    )

    check = CallerBoundaryCheck.from_environment(
        dry_run=True,
        passthrough_args=("--dangerously-skip-permissions",),
    )

    assert check.caller is None


@pytest.mark.parametrize("legacy_network", [None, "allow"], ids=["missing", "allow"])
def test_floored_legacy_caller_without_deny_has_no_network_floor(
    monkeypatch: pytest.MonkeyPatch,
    legacy_network: str | None,
) -> None:
    payload: dict[str, object] = {
        "pid_namespace": None,
        "sandbox": "workspace-write",
        "spawn": True,
    }
    if legacy_network is not None:
        payload["network"] = legacy_network
    monkeypatch.setenv(
        CALLER_BOUNDARY_ENV,
        json.dumps(payload),
    )

    check = CallerBoundaryCheck.from_environment(dry_run=True)

    assert check.caller is not None
    assert not check.caller.network_deny_floor


@pytest.mark.parametrize(
    ("authored_network", "effective_network", "expected_floor"),
    [
        (None, "allow", False),
        ("allow", "allow", False),
        ("deny", "allow", False),
        ("deny", "deny", True),
    ],
)
def test_caller_boundary_records_only_an_enforced_authored_deny(
    authored_network: NetworkMode | None,
    effective_network: NetworkMode,
    expected_floor: bool,
) -> None:
    boundary = CallerBoundary.from_policy(
        PermissionPolicy(sandbox="workspace-write", network=authored_network),
        effective_network=effective_network,
        pid_namespace=None,
    )

    assert boundary.network_deny_floor is expected_floor
    assert ("network" in json.loads(boundary.to_env_value())) is expected_floor


@pytest.mark.parametrize(
    "child_policy",
    [
        ResolvedExecutionPolicy(sandbox="danger-full-access"),
        ResolvedExecutionPolicy(),
    ],
)
def test_spawn_create_caller_guardrail_refuses_weaker_policy_before_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_policy: ResolvedExecutionPolicy,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    meridian_home = tmp_path / "meridian-home"
    monkeypatch.setenv("MERIDIAN_HOME", meridian_home.as_posix())
    _set_caller_guardrail(monkeypatch)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-haiku-5-5",
        harness=HarnessId.CLAUDE,
        execution_policy=child_policy,
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="claude-haiku-5-5",
            harness="claude",
            project_root=project_root.as_posix(),
            dry_run=False,
        )
    )

    assert result.status == "failed"
    assert result.error == "pre_init_failed"
    assert result.spawn_id is None
    assert not meridian_home.exists()


def test_spawn_create_caller_guardrail_requires_spawn_capability_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    _set_caller_guardrail(
        monkeypatch,
        has_spawn_capability=False,
    )

    def _must_not_resolve(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("delegation precondition must refuse before Mars")

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _must_not_resolve)
    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="claude-haiku-5-5",
            harness="claude",
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "failed"
    assert result.error == "pre_init_failed"
    assert result.spawn_id is None


def test_spawn_create_caller_guardrail_rejects_passthrough_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    _set_caller_guardrail(monkeypatch)

    def _must_not_resolve(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("delegation precondition must refuse before Mars")

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _must_not_resolve)
    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="claude-haiku-5-5",
            harness="claude",
            project_root=project_root.as_posix(),
            dry_run=True,
            passthrough_args=("--dangerously-skip-permissions",),
        )
    )

    assert result.status == "failed"
    assert result.error == "pre_init_failed"
    assert result.spawn_id is None


def test_spawn_create_caller_guardrail_allows_stronger_child_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    _set_caller_guardrail(monkeypatch)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-haiku-5-5",
        harness=HarnessId.CLAUDE,
        execution_policy=ResolvedExecutionPolicy(
            sandbox="read-only",
            approval="never",
        ),
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="claude-haiku-5-5",
            harness="claude",
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"


@pytest.mark.parametrize(
    ("harness", "child_network", "approval", "expected_status", "expected_message"),
    [
        (
            HarnessId.CLAUDE,
            "allow",
            "default",
            "failed",
            "set `network: deny` on the child profile",
        ),
        (HarnessId.CLAUDE, "deny", "default", "dry-run", None),
        (HarnessId.CLAUDE, "deny", "never", "dry-run", None),
        (
            HarnessId.OPENCODE,
            "deny",
            "default",
            "failed",
            "declares `network: deny`, but its harness or native override does not enforce it",
        ),
    ],
)
def test_spawn_create_caller_guardrail_compares_effective_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: HarnessId,
    child_network: NetworkMode,
    approval: str,
    expected_status: str,
    expected_message: str | None,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    _set_caller_guardrail(monkeypatch, network="deny")
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-haiku-5-5",
        harness=harness,
        execution_policy=ResolvedExecutionPolicy(
            sandbox="workspace-write",
            network=child_network,
            approval=approval,
        ),
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="claude-haiku-5-5",
            harness=harness.value,
            project_root=project_root.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == expected_status
    if expected_message is not None:
        assert result.message is not None
        assert expected_message in result.message


def test_spawn_create_caller_guardrail_does_not_restrict_child_workspace_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    _set_caller_guardrail(monkeypatch)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-haiku-5-5",
        harness=HarnessId.CLAUDE,
        execution_policy=ResolvedExecutionPolicy(sandbox="workspace-write"),
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="claude-haiku-5-5",
            harness="claude",
            project_root=project_root.as_posix(),
            task_dir=outside.as_posix(),
            dry_run=True,
        )
    )

    assert result.status == "dry-run"
    assert result.task_cwd == outside.as_posix()


def test_spawn_create_caller_guardrail_allows_harness_home_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    _set_caller_guardrail(monkeypatch)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-haiku-5-5",
        harness=HarnessId.CLAUDE,
        execution_policy=ResolvedExecutionPolicy(
            sandbox="read-only",
            approval="default",
        ),
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="claude-haiku-5-5",
            harness="claude",
            project_root=project_root.as_posix(),
            dry_run=True,
            launch_policy_snapshot=LaunchPolicySnapshot(
                model="claude-haiku-5-5",
                harness="claude",
                env={"CLAUDE_CONFIG_DIR": "/tmp/owned"},
                execution_policy=ResolvedExecutionPolicy(sandbox="read-only"),
            ),
        )
    )

    assert result.status == "dry-run"


def test_spawn_create_without_caller_guardrail_does_not_clamp_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.delenv(CALLER_BOUNDARY_ENV, raising=False)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-haiku-5-5",
        harness=HarnessId.CLAUDE,
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="claude-haiku-5-5",
            harness="claude",
            project_root=project_root.as_posix(),
            dry_run=True,
            env=("LD_PRELOAD=/tmp/user-choice.so",),
        )
    )

    assert result.status == "dry-run"


def test_spawn_create_unexpected_pre_init_exception_logs_traceback_and_distinct_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")

    def _raise_build_create_payload(*_args: object, **_kwargs: object) -> object:
        raise TypeError("bug in launch composition")

    monkeypatch.setattr(spawn_api, "build_create_payload", _raise_build_create_payload)

    with capture_logs() as logs:
        result = spawn_api.spawn_create_sync(
            SpawnCreateInput(
                prompt="run",
                model="gpt-5.4",
                harness="opencode",
                project_root=project_root.as_posix(),
            ),
            )

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

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="",
            model="gpt-5.4",
            harness="opencode",
            project_root=project_root.as_posix(),
            task_dir=(tmp_path / "task").as_posix(),
            work="browser-investigation",
        ),
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


def test_spawn_create_stale_ambient_work_falls_back_with_warning(
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
    work = work_repository.create_work_item(project_state_dir, "stale-work", "", None)
    stale = tmp_path / "removed-worktree"
    work_repository.update_work_item_task_dir(
        project_state_dir, work.name, task_dir=stale.as_posix()
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
    assert result.task_cwd == project_root.resolve().as_posix()
    assert result.task_cwd_source == "ambient-work-authority-root"
    assert result.task_cwd_work_item == work.name
    assert result.warning is not None
    assert stale.as_posix() in result.warning


def test_spawn_create_explicit_task_dir_wins_before_stale_ambient_derivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    explicit = tmp_path / "explicit-worktree"
    explicit.mkdir()
    monkeypatch.chdir(project_root)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.CODEX,
    )
    project_state_dir = resolve_project_paths(project_root).root_dir
    work = work_repository.create_work_item(project_state_dir, "stale-work", "", None)
    stale = tmp_path / "removed-worktree"
    work_repository.update_work_item_task_dir(
        project_state_dir, work.name, task_dir=stale.as_posix()
    )

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4-mini",
            project_root=project_root.as_posix(),
            task_dir=explicit.as_posix(),
            dry_run=True,
        ),
        ctx=RuntimeContext(work_id=work.name),
    )

    assert result.status == "dry-run"
    assert result.task_cwd == explicit.resolve().as_posix()
    assert result.task_cwd_source == "explicit-task-dir"
    assert result.warning is None or stale.as_posix() not in result.warning


def test_spawn_create_unresolvable_work_task_dir_has_distinct_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    runtime = build_runtime(project_root)
    project_paths = resolve_project_paths(project_root)
    work = work_repository.create_work_item(
        project_paths.root_dir, "unresolvable-work", "", None
    )
    stale = tmp_path / "removed-worktree"
    work_repository.update_work_item_task_dir(
        project_paths.root_dir,
        work.name,
        task_dir=stale.as_posix(),
    )
    missing_root = tmp_path / "removed-project-root"
    missing_authority = runtime.authority.model_copy(update={"project_root": missing_root})
    monkeypatch.setattr(
        spawn_api,
        "inspect_runtime_authority_for_read",
        lambda _project_root: missing_authority,
    )
    monkeypatch.setattr(
        spawn_api,
        "plan_runtime_authority_for_write",
        lambda _project_root: missing_authority,
    )
    monkeypatch.setattr(spawn_api, "resolve_project_paths", lambda _root: project_paths)
    monkeypatch.setattr(spawn_prepare, "resolve_project_paths", lambda _root: project_paths)

    result = spawn_api.spawn_create_sync(
        SpawnCreateInput(
            prompt="run",
            model="gpt-5.4-mini",
            project_root=missing_root.as_posix(),
            work=work.name,
            dry_run=True,
        )
    )

    assert result.status == "failed"
    assert result.error == "work_task_dir_missing"
    assert stale.as_posix() in result.message
    assert missing_root.as_posix() in result.message
