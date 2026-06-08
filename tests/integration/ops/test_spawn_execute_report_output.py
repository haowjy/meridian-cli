"""Foreground spawn execution output/report boundary tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import meridian.lib.ops.spawn.execute as execute_module
from meridian.lib.config.settings import load_config
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.ops.runtime import (
    OperationRuntime,
    build_runtime_from_root_and_config,
    resolve_runtime_authority_for_write,
)
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.state import spawn_store

if TYPE_CHECKING:
    import pytest

# qa-validated: spawn-return-report


def _build_test_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OperationRuntime:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    authority = resolve_runtime_authority_for_write(project_root)
    assert authority.runtime_root is not None
    config = load_config(project_root, authority=authority)
    return build_runtime_from_root_and_config(
        project_root,
        config,
        authority=authority,
    )


def test_execute_spawn_blocking_reads_report_and_does_not_print_running_preamble(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _build_test_runtime(tmp_path, monkeypatch)

    async def _fake_launch_prepared_spawn(**kwargs: object) -> int:
        spawn = cast("Any", kwargs["spawn"])
        runtime_root = Path(cast("Path", kwargs["runtime_root"]))
        spawn_id = str(spawn.spawn_id)
        report_path = runtime_root / "spawns" / spawn_id / "report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("fake report body\n", encoding="utf-8")
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            "succeeded",
            0,
            origin="runner",
            duration_secs=1.25,
        )
        return 0

    monkeypatch.setattr(execute_module, "launch_prepared_spawn", _fake_launch_prepared_spawn)

    result = execute_module.execute_spawn_blocking(
        payload=SpawnCreateInput(prompt="run"),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="codex",
            agent="coder",
        ),
        runtime=runtime,
    )

    captured = capsys.readouterr()
    assert '{"spawn_id":' not in captured.out
    assert '"status": "running"' not in captured.out
    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.report == "fake report body"
    assert result.duration_secs == 1.25
    assert result.format_text().endswith(
        "fake report body\n\nTranscript: meridian session log " + str(result.spawn_id)
    )


def test_execute_spawn_blocking_pre_init_failure_returns_failed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _build_test_runtime(tmp_path, monkeypatch)

    def _raise_project_paths(**kwargs: object) -> object:
        raise ValueError("harness binary not found")

    def _fail_init(**kwargs: object) -> object:
        raise AssertionError("_init_spawn should not be called")

    monkeypatch.setattr(execute_module, "resolve_project_config_paths", _raise_project_paths)
    monkeypatch.setattr(execute_module, "_init_spawn", _fail_init)

    result = execute_module.execute_spawn_blocking(
        payload=SpawnCreateInput(prompt="run"),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="opencode",
            agent="browser-prober",
            task_cwd="/tmp/browser-work",
            task_cwd_source="explicit-task-dir",
            task_cwd_work_item="browser-investigation",
        ),
        runtime=runtime,
    )

    assert result.status == "failed"
    assert result.spawn_id is None
    assert result.error == "pre_init_failed"
    assert result.exit_code == 1
    assert result.model == "gpt-5.4"
    assert result.harness_id == "opencode"
    assert result.message is not None
    assert "harness binary not found" in result.message
    assert result.to_wire()["message"] == result.message
    assert result.to_wire()["model"] == "gpt-5.4"
    assert result.to_wire()["harness_id"] == "opencode"
    assert result.to_wire()["task_cwd_source"] == "explicit-task-dir"
    assert result.to_wire()["task_cwd_work_item"] == "browser-investigation"


def test_execute_spawn_background_pre_init_failure_returns_failed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _build_test_runtime(tmp_path, monkeypatch)

    def _raise_project_paths(**kwargs: object) -> object:
        raise PermissionError("permission denied resolving project")

    def _fail_init(**kwargs: object) -> object:
        raise AssertionError("_init_spawn should not be called")

    monkeypatch.setattr(execute_module, "resolve_project_config_paths", _raise_project_paths)
    monkeypatch.setattr(execute_module, "_init_spawn", _fail_init)

    result = execute_module.execute_spawn_background(
        payload=SpawnCreateInput(prompt="run", background=True),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="opencode",
            agent="browser-prober",
            task_cwd="/tmp/browser-work",
            task_cwd_source="explicit-task-dir",
            task_cwd_work_item="browser-investigation",
        ),
        runtime=runtime,
    )

    assert result.status == "failed"
    assert result.spawn_id is None
    assert result.error == "pre_init_failed"
    assert result.exit_code == 1
    assert result.model == "gpt-5.4"
    assert result.harness_id == "opencode"
    assert result.message is not None
    assert "permission denied resolving project" in result.message
    assert result.to_wire()["message"] == result.message
    assert result.to_wire()["model"] == "gpt-5.4"
    assert result.to_wire()["harness_id"] == "opencode"
    assert result.to_wire()["task_cwd_source"] == "explicit-task-dir"
    assert result.to_wire()["task_cwd_work_item"] == "browser-investigation"
