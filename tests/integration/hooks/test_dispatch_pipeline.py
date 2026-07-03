from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import structlog
from structlog.testing import capture_logs

import meridian.lib.hooks.dispatch as _dispatch_mod
from meridian.lib.hooks.config import HooksConfig
from meridian.lib.hooks.dispatch import BUILTIN_HOOKS, HookDispatcher
from meridian.lib.hooks.registry import HookRegistry
from meridian.lib.hooks.types import Hook, HookContext, HookEventName, HookOutcome, HookResult


class NoopIntervalTracker:
    def should_run(self, hook_name: str, interval: str) -> bool:
        return True

    def mark_run(self, hook_name: str) -> None:
        return None


class RecordingExternalRunner:
    def __init__(self, *, outcomes: dict[str, HookResult]) -> None:
        self._outcomes = outcomes
        self.call_order: list[str] = []

    def run(self, hook: Hook, context: HookContext, *, timeout_secs: int) -> HookResult:
        self.call_order.append(hook.name)
        return self._outcomes[hook.name]


class RecordingBuiltin:
    name = "recording"
    requirements = ("git",)
    default_events = ("spawn.finalized",)
    default_interval = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def check_requirements(self) -> tuple[bool, str | None]:
        return True, None

    def execute(self, context: HookContext, config: Hook) -> HookResult:
        self.calls.append((config.name, context.event_name))
        return HookResult(
            hook_name=config.name,
            event=context.event_name,
            outcome="success",
            success=True,
            exit_code=0,
            duration_ms=1,
            stdout="builtin-ok",
        )


def _context(event_name: HookEventName = "spawn.finalized") -> HookContext:
    return HookContext(
        event_name=event_name,
        event_id=uuid4(),
        timestamp="2026-04-20T00:00:00+00:00",
        project_root="/repo",
        runtime_root="/repo/.meridian",
        spawn_id="p123",
        spawn_status="success",
        spawn_agent="reviewer",
    )


def _result(name: str, *, outcome: HookOutcome = "success") -> HookResult:
    return HookResult(
        hook_name=name,
        event="spawn.finalized",
        outcome=outcome,
        success=outcome == "success",
        error=None if outcome == "success" else "hook failed",
    )


def _python_command(script_path: Path, *args: str) -> str:
    return subprocess.list2cmdline([sys.executable, str(script_path), *args])


def _exec_python_command(script_path: Path, *args: str) -> str:
    command = _python_command(script_path, *args)
    if sys.platform == "win32":
        return command
    return f"exec {command}"


def test_dispatch_pipeline_uses_registry_ordering(tmp_path: Path) -> None:
    hooks = HooksConfig(
        hooks=(
            Hook(
                name="local-low",
                event="spawn.finalized",
                source="local",
                command="./local-low.sh",
                priority=0,
            ),
            Hook(
                name="project-mid",
                event="spawn.finalized",
                source="project",
                command="./project-mid.sh",
                priority=1,
            ),
            Hook(
                name="user-high",
                event="spawn.finalized",
                source="user",
                command="./user-high.sh",
                priority=10,
            ),
        )
    )
    registry = HookRegistry(Path(tmp_path), hooks_config=hooks)
    runner = RecordingExternalRunner(
        outcomes={
            "local-low": _result("local-low"),
            "project-mid": _result("project-mid"),
            "user-high": _result("user-high"),
        }
    )
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=registry,
        interval_tracker=NoopIntervalTracker(),
        external_runner=runner,
    )

    results = dispatcher.fire(_context())

    assert runner.call_order == ["user-high", "project-mid", "local-low"]
    assert [result.hook_name for result in results] == ["user-high", "project-mid", "local-low"]


def test_dispatch_pipeline_isolates_failures_and_continues_post_event(tmp_path: Path) -> None:
    hooks = HooksConfig(
        hooks=(
            Hook(
                name="first",
                event="spawn.finalized",
                source="project",
                command="./first.sh",
                priority=10,
            ),
            Hook(
                name="second",
                event="spawn.finalized",
                source="project",
                command="./second.sh",
                priority=5,
            ),
            Hook(
                name="third",
                event="spawn.finalized",
                source="project",
                command="./third.sh",
                priority=0,
            ),
        )
    )
    registry = HookRegistry(Path(tmp_path), hooks_config=hooks)
    runner = RecordingExternalRunner(
        outcomes={
            "first": _result("first"),
            "second": _result("second", outcome="failure"),
            "third": _result("third"),
        }
    )
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=registry,
        interval_tracker=NoopIntervalTracker(),
        external_runner=runner,
    )

    results = dispatcher.fire(_context())

    assert runner.call_order == ["first", "second", "third"]
    assert [result.outcome for result in results] == ["success", "failure", "success"]


def test_dispatch_pipeline_executes_builtin_hook_in_process(tmp_path: Path) -> None:
    hooks = HooksConfig(
        hooks=(
            Hook(
                name="builtin-recorder",
                event="spawn.finalized",
                source="project",
                command=None,
                builtin="recording",
            ),
        )
    )
    registry = HookRegistry(Path(tmp_path), hooks_config=hooks)
    builtin = RecordingBuiltin()
    dispatcher = HookDispatcher(
        tmp_path,
        tmp_path / "state",
        registry=registry,
        interval_tracker=NoopIntervalTracker(),
        builtin_hooks={**BUILTIN_HOOKS, "recording": builtin},
    )

    results = dispatcher.fire(_context())

    assert builtin.calls == [("builtin-recorder", "spawn.finalized")]
    assert len(results) == 1
    assert results[0].outcome == "success"
    assert results[0].stdout == "builtin-ok"
    assert results[0].exit_code == 0


def test_dispatch_pipeline_persists_interval_state_across_runs(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "state"
    marker = tmp_path / "runs.jsonl"
    script = tmp_path / "record_run.py"
    script.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "payload = json.loads(sys.stdin.read())\n"
        "Path(sys.argv[1]).write_text(json.dumps(payload), encoding='utf-8')\n",
        encoding="utf-8",
    )

    hooks = HooksConfig(
        hooks=(
            Hook(
                name="throttled-recorder",
                event="spawn.finalized",
                source="project",
                command=_python_command(script, str(marker)),
                interval="1h",
            ),
        )
    )
    registry = HookRegistry(project_root, hooks_config=hooks)

    first_dispatcher = HookDispatcher(project_root, runtime_root, registry=registry)
    first_results = first_dispatcher.fire(
        _context(),
    )

    hook_state_path = runtime_root / "hook-state.json"
    assert [result.outcome for result in first_results] == ["success"]
    assert marker.exists() is True
    assert hook_state_path.exists() is True
    persisted_state = json.loads(hook_state_path.read_text(encoding="utf-8"))
    assert "throttled-recorder" in persisted_state

    second_dispatcher = HookDispatcher(project_root, runtime_root, registry=registry)
    second_results = second_dispatcher.fire(_context())

    assert [result.outcome for result in second_results] == ["skipped"]
    assert second_results[0].skip_reason == "throttled"
    assert not list(hook_state_path.parent.glob(".hook-state.json.*.tmp"))


def test_dispatch_pipeline_times_out_then_continues_to_later_hooks(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "state"
    term_marker = tmp_path / "term.txt"
    success_marker = tmp_path / "success.txt"

    hanging_script = tmp_path / "hang.py"
    hanging_script.write_text(
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "marker = Path(sys.argv[1])\n"
        "def _handle_term(signum, frame):\n"
        "    marker.write_text('terminated', encoding='utf-8')\n"
        "    while True:\n"
        "        time.sleep(0.1)\n"
        "signal.signal(signal.SIGTERM, _handle_term)\n"
        "while True:\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    success_script = tmp_path / "success.py"
    success_script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )

    hooks = HooksConfig(
        hooks=(
            Hook(
                name="slow-first",
                event="spawn.finalized",
                source="project",
                command=_exec_python_command(hanging_script, str(term_marker)),
                timeout_secs=1,
                priority=10,
            ),
            Hook(
                name="fast-second",
                event="spawn.finalized",
                source="project",
                command=_python_command(success_script, str(success_marker)),
                priority=0,
            ),
        )
    )
    registry = HookRegistry(project_root, hooks_config=hooks)
    dispatcher = HookDispatcher(project_root, runtime_root, registry=registry)

    results = dispatcher.fire(_context())

    assert [result.hook_name for result in results] == ["slow-first", "fast-second"]
    assert [result.outcome for result in results] == ["timeout", "success"]
    assert success_marker.read_text(encoding="utf-8") == "ok"
    if sys.platform != "win32":
        deadline = time.monotonic() + 3
        while not term_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert term_marker.read_text(encoding="utf-8") == "terminated"


def test_dispatch_pipeline_logs_fail_open_failures_and_completion_metadata(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "state"
    success_marker = tmp_path / "success.txt"

    fail_script = tmp_path / "fail.py"
    fail_script.write_text("raise SystemExit(7)\n", encoding="utf-8")
    success_script = tmp_path / "success.py"
    success_script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )

    hooks = HooksConfig(
        hooks=(
            Hook(
                name="fails-first",
                event="spawn.finalized",
                source="project",
                command=_python_command(fail_script),
                priority=10,
            ),
            Hook(
                name="runs-after-failure",
                event="spawn.finalized",
                source="project",
                command=_python_command(success_script, str(success_marker)),
                priority=0,
            ),
        )
    )
    registry = HookRegistry(project_root, hooks_config=hooks)
    dispatcher = HookDispatcher(project_root, runtime_root, registry=registry)

    # Rebind module-level logger so capture_logs intercepts it even when an
    # earlier test triggered structlog's cache_logger_on_first_use caching.
    _saved_logger = _dispatch_mod.logger
    with capture_logs() as logs:
        _dispatch_mod.logger = structlog.get_logger(_dispatch_mod.__name__)
        try:
            results = dispatcher.fire(_context())
        finally:
            _dispatch_mod.logger = _saved_logger

    assert [result.outcome for result in results] == ["failure", "success"]
    assert success_marker.read_text(encoding="utf-8") == "ok"

    failure_log = next((log for log in logs if log["event"] == "hook_execution_failed"), None)
    success_log = next(
        (
            log
            for log in logs
            if log.get("event") == "hook_execution_finished"
            and log.get("hook") == "runs-after-failure"
        ),
        None,
    )

    assert failure_log is not None, (
        f"Expected hook_execution_failed log entry. "
        f"Captured events: {[log.get('event') for log in logs]}"
    )
    assert success_log is not None, (
        "Expected success completion log entry for runs-after-failure. "
        f"Captured events: {[log.get('event') for log in logs]}"
    )

    assert failure_log["hook"] == "fails-first"
    assert failure_log["hook_event"] == "spawn.finalized"
    assert failure_log["error"] == "Exited with code 7."
    assert failure_log["fail_open"] is True
    assert "error_type" in failure_log
    assert success_log["hook"] == "runs-after-failure"
    assert success_log["hook_event"] == "spawn.finalized"
    assert isinstance(success_log["duration_ms"], int)
    assert "exit_code" in success_log
