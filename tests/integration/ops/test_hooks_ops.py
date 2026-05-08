from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import meridian.lib.ops.hooks as hooks_ops
from meridian.lib.hooks.interval import IntervalTracker


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_HOOKS_ENABLED", raising=False)
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")


def _python_command(script_path: Path, *args: str) -> str:
    return subprocess.list2cmdline([sys.executable, str(script_path), *args])


def _write_hook_recorder(path: Path) -> None:
    path.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "payload = json.loads(sys.stdin.read())\n"
        "target = Path(sys.argv[1])\n"
        "target.parent.mkdir(parents=True, exist_ok=True)\n"
        "with target.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(payload) + '\\n')\n",
        encoding="utf-8",
    )


def test_hooks_list_returns_registered_hooks_with_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    user_config = tmp_path / "user-config.toml"
    user_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())

    (project_root / "meridian.toml").write_text(
        "[[hooks]]\n"
        "name = 'record-finalized'\n"
        "event = 'spawn.finalized'\n"
        "command = './scripts/record.sh'\n"
        "\n"
        "[[hooks]]\n"
        "name = 'disabled-created'\n"
        "event = 'spawn.created'\n"
        "command = './scripts/created.sh'\n"
        "enabled = false\n"
        "\n"
        "[[hooks]]\n"
        "name = 'git-autosync'\n"
        "builtin = 'git-autosync'\n"
        "remote = 'https://github.com/acme/project.git'\n",
        encoding="utf-8",
    )

    output = hooks_ops.hooks_list_sync(
        hooks_ops.HookListInput(project_root=project_root.as_posix())
    )

    by_name = {hook.name: hook for hook in output.hooks}
    assert by_name["record-finalized"].hook_type == "external"
    assert by_name["record-finalized"].status == "enabled"
    assert by_name["record-finalized"].registration == "config"
    assert by_name["record-finalized"].auto_registered is False
    assert by_name["disabled-created"].status == "disabled"
    assert by_name["git-autosync"].hook_type == "builtin"
    assert by_name["git-autosync"].registration == "config"


def test_hooks_check_reports_missing_builtin_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeBuiltin:
        def __init__(self, *, requirements: tuple[str, ...], ok: bool, error: str | None) -> None:
            self.requirements = requirements
            self._ok = ok
            self._error = error

        def check_requirements(self) -> tuple[bool, str | None]:
            return self._ok, self._error

    monkeypatch.setattr(
        hooks_ops,
        "BUILTIN_HOOKS",
        {
            "alpha": _FakeBuiltin(requirements=("git",), ok=True, error=None),
            "beta": _FakeBuiltin(requirements=("python",), ok=False, error="python missing"),
        },
    )

    output = hooks_ops.hooks_check_sync(hooks_ops.HookCheckInput())

    assert output.ok is False
    by_name = {check.name: check for check in output.checks}
    assert by_name["alpha"].ok is True
    assert by_name["beta"].ok is False
    assert by_name["beta"].error == "python missing"


def test_hooks_run_bypasses_interval_throttling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    runtime_root = project_root / ".meridian"

    user_config = tmp_path / "user-config.toml"
    user_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())

    marker = tmp_path / "manual-run-events.jsonl"
    recorder = tmp_path / "record_hook.py"
    _write_hook_recorder(recorder)
    command = _python_command(recorder, marker.as_posix())

    (project_root / "meridian.toml").write_text(
        f"[[hooks]]\n"
        f"name = 'record-finalized'\n"
        f"event = 'spawn.finalized'\n"
        f"command = '{command}'\n"
        f"interval = '10m'\n",
        encoding="utf-8",
    )

    # Seed interval state to force throttling under normal dispatch.
    IntervalTracker(runtime_root).mark_run("record-finalized")

    output = hooks_ops.hooks_run_sync(
        hooks_ops.HookRunInput(name="record-finalized", project_root=project_root.as_posix())
    )

    assert output.hook == "record-finalized"
    assert output.result.success is True
    payloads = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]
    assert len(payloads) == 1
    assert payloads[0]["event_name"] == "spawn.finalized"


def test_hooks_run_accepts_event_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    runtime_root = project_root / ".meridian"

    user_config = tmp_path / "user-config.toml"
    user_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())

    marker = tmp_path / "manual-event-override.jsonl"
    recorder = tmp_path / "record_hook.py"
    _write_hook_recorder(recorder)
    command = _python_command(recorder, marker.as_posix())

    (project_root / "meridian.toml").write_text(
        f"[[hooks]]\nname = 'record-finalized'\nevent = 'spawn.finalized'\ncommand = '{command}'\n",
        encoding="utf-8",
    )

    output = hooks_ops.hooks_run_sync(
        hooks_ops.HookRunInput(
            name="record-finalized",
            event="work.done",
            project_root=project_root.as_posix(),
        )
    )

    assert output.event == "work.done"
    payloads = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]
    assert len(payloads) == 1
    assert payloads[0]["event_name"] == "work.done"


def test_hooks_resolve_returns_enabled_hooks_in_dispatch_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()

    user_config = tmp_path / "user.toml"
    user_config.write_text(
        "[[hooks]]\n"
        "name = 'user-high'\n"
        "event = 'work.done'\n"
        "command = './user.sh'\n"
        "priority = 10\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())
    (project_root / "meridian.toml").write_text(
        "[[hooks]]\n"
        "name = 'project-low'\n"
        "event = 'work.done'\n"
        "command = './project.sh'\n"
        "priority = -1\n"
        "\n"
        "[[hooks]]\n"
        "name = 'project-disabled'\n"
        "event = 'work.done'\n"
        "command = './skip.sh'\n"
        "enabled = false\n",
        encoding="utf-8",
    )

    output = hooks_ops.hooks_resolve_sync(
        hooks_ops.HookResolveInput(event="work.done", project_root=project_root.as_posix())
    )

    assert [item.name for item in output.hooks] == ["user-high", "project-low"]
