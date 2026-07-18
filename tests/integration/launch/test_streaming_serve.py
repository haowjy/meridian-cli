# qa-validated: test-suite-redesign
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian.cli import streaming_serve as streaming_serve_module
from meridian.lib.core.types import HarnessId
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.launch.resolve import resolve_startup_timeout_seconds
from meridian.lib.ops.runtime import resolve_runtime_root
from meridian.lib.state.spawn_store import get_spawn
from meridian.lib.streaming.spawn_manager import DrainOutcome
from tests.support.launch import stub_bundle_request_and_resolve


@pytest.fixture(autouse=True)
def _stub_launch_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
        harness_model="gpt-5.4",
    )


def _fake_launch_context(
    *,
    spawn_id: str,
    project_root: Path,
    child_cwd: Path,
    prompt: str = "projected prompt",
    system: str | None = None,
    config_snapshot: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        resolved_request=SimpleNamespace(
            model="gpt-5.4",
            harness="codex",
            agent="coder",
            prompt=prompt,
            launch_policy_snapshot=None,
            skills=(),
            skill_paths=(),
            execution_policy=ResolvedExecutionPolicy(),
            session=SimpleNamespace(requested_harness_session_id=None),
            agent_metadata={
                "appended_system_prompt": system,
                "session_agent_path": str(child_cwd / "agent.md"),
            },
            pi_task_ping_interval_seconds=None,
            pi_task_ping_reset_on_activity=None,
        ),
        project_root=project_root,
        control_root=project_root,
        task_cwd=child_cwd if child_cwd.resolve() != project_root.resolve() else None,
        runtime_root=resolve_runtime_root(project_root),
        runtime=SimpleNamespace(config_snapshot=config_snapshot),
        work_id=None,
        binding=SimpleNamespace(
            child_cwd=child_cwd,
            environment=SimpleNamespace(
                bind_env_overrides={
                    "MERIDIAN_SPAWN_ID": spawn_id,
                    "_MERIDIAN_PARENT_SPAWN_ID": "p-parent",
                    "EXTRA_ENV": "present",
                },
                final_env={
                    "PATH": "/usr/bin",
                    "HOME": "/home/tester",
                    "MERIDIAN_SPAWN_ID": spawn_id,
                    "_MERIDIAN_PARENT_SPAWN_ID": "p-parent",
                    "EXTRA_ENV": "present",
                },
            ),
            spec=SimpleNamespace(name="fake-spec"),
            run_params=SimpleNamespace(
                appended_system_prompt=system or "",
                interactive=False,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_streaming_serve_shutdown_finalizes_once_as_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_runtime_root(tmp_path)
    helper_calls: list[tuple[str, str]] = []

    async def _run_streaming_spawn(**kwargs: object) -> DrainOutcome:
        helper_calls.append((str(kwargs["spawn_id"]), str(kwargs["runtime_root"])))
        return DrainOutcome(status="cancelled", exit_code=1)

    monkeypatch.setattr(streaming_serve_module, "run_streaming_spawn", _run_streaming_spawn)

    await streaming_serve_module.streaming_serve("codex", "hello")

    assert helper_calls == [("p1", str(runtime_root))]
    row = get_spawn(runtime_root, "p1")
    assert row is not None
    assert row.status == "cancelled"
    assert row.terminal.exit_code == 1


@pytest.mark.asyncio
async def test_streaming_serve_start_failure_finalizes_failed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_runtime_root(tmp_path)

    async def _run_streaming_spawn(**kwargs: object) -> DrainOutcome:
        _ = kwargs
        raise RuntimeError("boom")

    monkeypatch.setattr(streaming_serve_module, "run_streaming_spawn", _run_streaming_spawn)

    with pytest.raises(RuntimeError, match="boom"):
        await streaming_serve_module.streaming_serve("codex", "hello")

    row = get_spawn(runtime_root, "p1")
    assert row is not None
    assert row.status == "failed"
    assert row.terminal.error == "boom"
    assert row.status == "failed"
    assert row.terminal.error == "boom"


@pytest.mark.asyncio
async def test_streaming_serve_debug_keeps_projected_connection_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_runtime_root(tmp_path)
    child_cwd = tmp_path / "child-cwd"
    child_cwd.mkdir()
    runner_calls: list[dict[str, object]] = []
    config_snapshot = {"timeouts": {"startup_timeout_minutes": 7.5}}

    def _bind_spawn_launch_context(**kwargs: object) -> SimpleNamespace:
        bindings = kwargs["bindings"]
        return _fake_launch_context(
            spawn_id=str(bindings.spawn_id),
            project_root=tmp_path,
            child_cwd=child_cwd,
            system="SYSTEM: projected",
            config_snapshot=config_snapshot,
        )

    async def _run_streaming_spawn(**kwargs: object) -> DrainOutcome:
        runner_calls.append(kwargs)
        return DrainOutcome(status="succeeded", exit_code=0)

    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.compose_spawn_launch_surface",
        lambda **kwargs: SimpleNamespace(request=kwargs["request"]),
    )
    monkeypatch.setattr(
        "meridian.lib.core.spawn_service.bind_spawn_launch_context",
        _bind_spawn_launch_context,
    )
    monkeypatch.setattr(streaming_serve_module, "run_streaming_spawn", _run_streaming_spawn)

    await streaming_serve_module.streaming_serve("codex", "hello", debug=True)

    assert len(runner_calls) == 1
    runner_call = runner_calls[0]
    config = runner_call["config"]
    assert str(runner_call["spawn_id"]) == "p1"
    assert runner_call["runtime_root"] == runtime_root
    assert runner_call["project_root"] == tmp_path
    assert runner_call["spec"].name == "fake-spec"
    assert config.spawn_id == "p1"
    assert config.prompt == "projected prompt"
    assert config.control_root == tmp_path
    assert config.task_cwd == child_cwd
    assert config.system == "SYSTEM: projected"
    assert config.child_env["PATH"] == "/usr/bin"
    assert config.child_env["HOME"] == "/home/tester"
    assert config.child_env["MERIDIAN_SPAWN_ID"] == "p1"
    assert config.child_env["_MERIDIAN_PARENT_SPAWN_ID"] == "p-parent"
    assert config.child_env["EXTRA_ENV"] == "present"
    assert config.debug_tracer is not None
    assert runner_call["startup_timeout_seconds"] == resolve_startup_timeout_seconds(
        config_snapshot=config_snapshot,
    )

    row = get_spawn(runtime_root, "p1")
    assert row is not None
    assert row.control_root == tmp_path.as_posix()
    assert row.task_cwd == child_cwd.as_posix()
    assert row.execution_cwd == child_cwd.as_posix()
    assert row.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reported_endpoint",
    [
        "unix:///tmp/.meridian/spawns/p1/control.sock",
        "tcp://127.0.0.1:43125",
    ],
)
async def test_streaming_serve_reports_platform_control_endpoint(
    reported_endpoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _run_streaming_spawn(**kwargs: object) -> DrainOutcome:
        callback = kwargs.get("on_control_endpoint_ready")
        assert callable(callback)
        callback(reported_endpoint)
        return DrainOutcome(status="succeeded", exit_code=0)

    monkeypatch.setattr(streaming_serve_module, "run_streaming_spawn", _run_streaming_spawn)

    await streaming_serve_module.streaming_serve("codex", "hello")

    output = capsys.readouterr().out
    assert f"Control endpoint: {reported_endpoint}" in output
