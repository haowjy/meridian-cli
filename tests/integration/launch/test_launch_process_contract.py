# qa-validated: test-suite-redesign
"""Contract and low-level bootstrap tests for primary process launch.

Tests that verify the execute_primary_process contract logic (bootstrap mode,
attach failure policy) and the SubprocessProcessLauncher output capture.
These tests do NOT spin up a real harness process.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import BootstrapMode
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.process import runner as process_runner
from meridian.lib.launch.process.ports import PRIMARY_STDERR_LOG_PATH_ENV, LaunchedProcess
from meridian.lib.launch.process.primary_attach import PrimaryAttachError
from meridian.lib.launch.process.subprocess_launcher import SubprocessProcessLauncher
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def test_subprocess_launcher_captures_output_log(tmp_path: Path) -> None:
    output_log_path = tmp_path / "history.jsonl"
    running = SubprocessProcessLauncher().start(
        command=(
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stdout.write('line-1\\n');"
                "sys.stdout.flush();"
                "sys.stderr.write('line-2\\n');"
                "sys.stderr.flush()"
            ),
        ),
        cwd=tmp_path,
        env=dict(os.environ),
        output_log_path=output_log_path,
    )
    launched = running.wait()

    assert launched.exit_code == 0
    assert output_log_path.read_text(encoding="utf-8").splitlines() == ["line-1", "line-2"]


@pytest.mark.parametrize("capture_output", (False, True))
def test_subprocess_launcher_cancel_wait_releases_pipe_relays(
    tmp_path: Path,
    capture_output: bool,
) -> None:
    env = dict(os.environ)
    output_log_path = tmp_path / "output.log" if capture_output else None
    if not capture_output:
        env[PRIMARY_STDERR_LOG_PATH_ENV] = str(tmp_path / "stderr.log")
    running = SubprocessProcessLauncher().start(
        command=(sys.executable, "-c", "import time;time.sleep(60)"),
        cwd=tmp_path,
        env=env,
        output_log_path=output_log_path,
    )
    results: list[LaunchedProcess] = []
    wait_started = threading.Event()
    wait_finished = threading.Event()

    def _wait() -> None:
        wait_started.set()
        results.append(running.wait())
        wait_finished.set()

    wait_thread = threading.Thread(target=_wait)
    wait_thread.start()
    assert wait_started.wait(timeout=5.0)

    try:
        running.cancel_wait()
        assert wait_finished.wait(timeout=5.0)
        wait_thread.join()

        assert wait_thread.is_alive() is False
        assert [result.exit_code for result in results] == [130]
    finally:
        running.terminate()
        running.process.wait(timeout=5.0)


def test_execute_primary_process_uses_contract_bootstrap_mode_not_harness_id(
    tmp_path: Path,
) -> None:
    harness_registry = get_default_harness_registry()
    harness_contract = harness_registry.get_contract(HarnessId.CODEX).model_copy(
        update={
            "bootstrap": harness_registry.get_contract(HarnessId.CODEX).bootstrap.model_copy(
                update={"mode": BootstrapMode.SUBPROCESS_ONLY}
            )
        }
    )
    black_box_calls = 0

    class _Managed:
        def record_harness_session_id(self, _session_id: str) -> None:
            return None

    def _black_box(
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        on_child_started: Any,
    ) -> tuple[int, int]:
        nonlocal black_box_calls
        _ = (command, cwd, env, output_log_path)
        black_box_calls += 1
        if callable(on_child_started):
            on_child_started(111)
        return (0, 111)

    exit_code, managed_session_id = process_runner._execute_primary_process(
        harness_id=HarnessId.CODEX,
        primary_spawn_id=SpawnId("p-contract-blackbox"),
        log_dir=tmp_path,
        control_root=tmp_path,
        launch_cwd=tmp_path,
        task_cwd=None,
        child_env={},
        launch_spec=ResolvedLaunchSpec(
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            interactive=True,
        ),
        command=("codex",),
        harness_contract=harness_contract,
        managed=_Managed(),
        runtime_root=tmp_path,
        run_primary_process_with_capture_fn=_black_box,
        run_primary_attach_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("subprocess_only contract should bypass managed attach")
        ),
        on_running=lambda _pid: None,
    )

    assert black_box_calls == 1
    assert exit_code == 0
    assert managed_session_id is None


def test_execute_primary_process_uses_contract_attach_failure_policy_not_harness_id(
    tmp_path: Path,
) -> None:
    harness_registry = get_default_harness_registry()
    harness_contract = harness_registry.get_contract(HarnessId.CLAUDE).model_copy(
        update={
            "bootstrap": harness_registry.get_contract(HarnessId.CLAUDE).bootstrap.model_copy(
                update={
                    "mode": BootstrapMode.MANAGED_PRIMARY_ATTACH,
                    "primary_attach_failure_policy": "fallback_to_blackbox",
                }
            )
        }
    )
    black_box_calls = 0

    class _Managed:
        def record_harness_session_id(self, _session_id: str) -> None:
            return None

    def _black_box(
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        on_child_started: Any,
    ) -> tuple[int, int]:
        nonlocal black_box_calls
        _ = (command, cwd, env, output_log_path)
        black_box_calls += 1
        if callable(on_child_started):
            on_child_started(222)
        return (0, 222)

    exit_code, managed_session_id = process_runner._execute_primary_process(
        harness_id=HarnessId.CLAUDE,
        primary_spawn_id=SpawnId("p-contract-fallback"),
        log_dir=tmp_path,
        control_root=tmp_path,
        launch_cwd=tmp_path,
        task_cwd=None,
        child_env={},
        launch_spec=ResolvedLaunchSpec(
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
            interactive=True,
        ),
        command=("claude",),
        harness_contract=harness_contract,
        managed=_Managed(),
        runtime_root=tmp_path,
        run_primary_process_with_capture_fn=_black_box,
        run_primary_attach_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            PrimaryAttachError("fallback please")
        ),
        on_running=lambda _pid: None,
    )

    assert black_box_calls == 1
    assert exit_code == 0
    assert managed_session_id is None
