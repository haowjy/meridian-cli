from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from meridian.lib.hooks.runner import ExternalHookRunner
from meridian.lib.hooks.types import Hook, HookContext

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _python_command(script_path: Path) -> str:
    return subprocess.list2cmdline([sys.executable, str(script_path)])


def _context(project_root: Path, runtime_root: Path) -> HookContext:
    return HookContext(
        event_name="spawn.finalized",
        event_id=uuid4(),
        timestamp="2026-04-19T12:00:00+00:00",
        project_root=str(project_root),
        runtime_root=str(runtime_root),
        spawn_id="p123",
        spawn_status="success",
        spawn_agent="reviewer",
        spawn_model="gpt-5.3-codex",
    )


def _external_hook(command: str) -> Hook:
    return Hook(
        name="notify",
        event="spawn.finalized",
        source="project",
        command=command,
    )


def test_external_runner_sets_cwd_env_and_json_stdin(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "state"

    script = tmp_path / "echo_context.py"
    script.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "print(os.getcwd())\n"
        "print(os.environ['MERIDIAN_HOOK_EVENT'])\n"
        "print(os.environ['MERIDIAN_HOOK_EVENT_ID'])\n"
        "print(os.environ['MERIDIAN_PROJECT_DIR'])\n"
        "print(payload['runtime_root'])\n"
        "print(os.environ['MERIDIAN_SPAWN_ID'])\n"
        "print(os.environ['MERIDIAN_SPAWN_STATUS'])\n"
        "print(os.environ['MERIDIAN_SPAWN_AGENT'])\n"
        "print(os.environ['MERIDIAN_SPAWN_MODEL'])\n"
        "print(payload['event_name'])\n"
        "print(payload['spawn']['id'])\n",
        encoding="utf-8",
    )

    runner = ExternalHookRunner(project_root)
    context = _context(project_root, runtime_root)
    hook = _external_hook(_python_command(script))

    result = runner.run(hook, context, timeout_secs=5)

    assert result.outcome == "success"
    assert result.success is True
    assert result.exit_code == 0
    assert result.stdout is not None
    lines = result.stdout.strip().splitlines()
    assert lines[0] == str(project_root.resolve())
    assert lines[1] == "spawn.finalized"
    assert lines[2] == str(context.event_id)
    assert lines[3] == str(project_root.resolve())
    assert lines[4] == str(runtime_root)
    assert lines[5] == "p123"
    assert lines[6:9] == ["success", "reviewer", "gpt-5.3-codex"]
    assert lines[9] == "spawn.finalized"
    assert lines[10] == "p123"


def test_external_runner_captures_nonzero_exit_and_1kb_tails(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "state"

    script = tmp_path / "emit_large_output.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('a' * 1600)\n"
        "sys.stderr.write('b' * 1600)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )

    runner = ExternalHookRunner(project_root)
    result = runner.run(
        _external_hook(_python_command(script)),
        _context(project_root, runtime_root),
        timeout_secs=5,
    )

    assert result.outcome == "failure"
    assert result.success is False
    assert result.exit_code == 7
    assert result.error == "Exited with code 7."
    assert result.stdout is not None
    assert result.stderr is not None
    assert len(result.stdout) == 1024
    assert len(result.stderr) == 1024
    assert result.stdout == "a" * 1024
    assert result.stderr == "b" * 1024


def test_external_runner_times_out_sleeping_hook(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "state"

    script = tmp_path / "sleep_forever.py"
    script.write_text(
        "import time\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    runner = ExternalHookRunner(project_root)
    result = runner.run(
        _external_hook(_python_command(script)),
        _context(project_root, runtime_root),
        timeout_secs=1,
    )

    assert result.outcome == "timeout"
    assert result.success is False
    assert result.error == "Timed out after 1s."


def test_external_runner_omits_null_context_variables(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "state"

    script = tmp_path / "check_missing_env.py"
    script.write_text(
        "import os\n"
        "print('MERIDIAN_SPAWN_ERROR' in os.environ)\n"
        "print('MERIDIAN_ACTIVE_WORK_ID' in os.environ)\n"
        "print('MERIDIAN_ACTIVE_WORK_DIR' in os.environ)\n",
        encoding="utf-8",
    )

    runner = ExternalHookRunner(project_root)
    result = runner.run(
        _external_hook(_python_command(script)),
        _context(project_root, runtime_root),
        timeout_secs=5,
    )

    assert result.outcome == "success"
    assert result.stdout is not None
    assert result.stdout.strip().splitlines() == ["False", "False", "False"]


def test_external_runner_short_circuits_when_hooks_disabled(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = tmp_path / "state"
    marker = tmp_path / "ran.txt"

    script = tmp_path / "should_not_run.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("MERIDIAN_HOOKS_ENABLED", "false")
    runner = ExternalHookRunner(project_root)
    result = runner.run(
        _external_hook(_python_command(script)),
        _context(project_root, runtime_root),
        timeout_secs=5,
    )

    assert result.outcome == "skipped"
    assert result.success is True
    assert result.skipped is True
    assert result.skip_reason == "hooks_disabled"
    assert marker.exists() is False
