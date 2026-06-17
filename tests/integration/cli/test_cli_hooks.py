import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

cli_main = importlib.import_module("meridian.cli.main")
hooks_cli = importlib.import_module("meridian.cli.hooks_commands")
ops_hooks = importlib.import_module("meridian.lib.ops.hooks")
runtime_ops = importlib.import_module("meridian.lib.ops.runtime")


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


def test_hooks_run_ignores_parent_project_and_runtime_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    parent_project = tmp_path / "parent-project"
    parent_project.mkdir()
    child_project = tmp_path / "child-project"
    child_project.mkdir()
    marker = tmp_path / "hook-events.jsonl"
    recorder = tmp_path / "record_hook.py"
    _write_hook_recorder(recorder)
    command = _python_command(recorder, marker.as_posix())
    (child_project / "meridian.toml").write_text(
        f"[[hooks]]\nname = 'record-finalized'\nevent = 'spawn.finalized'\ncommand = '{command}'\n",
        encoding="utf-8",
    )
    (parent_project / "meridian.toml").write_text("", encoding="utf-8")
    parent_runtime = tmp_path / "parent-runtime"
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", parent_project.as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", parent_runtime.as_posix())
    monkeypatch.chdir(child_project)

    monkeypatch.delenv("MERIDIAN_PROJECT_DIR")
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR")
    expected_roots = runtime_ops.resolve_roots(child_project.as_posix())
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", parent_project.as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", parent_runtime.as_posix())

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["hooks", "run", "record-finalized"])

    assert exc_info.value.code == 0
    payloads = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["project_root"] == child_project.resolve().as_posix()
    assert payload["runtime_root"] == expected_roots.runtime_root.as_posix()
    assert payload["runtime_root"] != parent_runtime.as_posix()


@pytest.mark.parametrize(
    "argv",
    [
        ["hooks"],
    ],
)
def test_nested_manual_hooks_ignore_inherited_roots_during_handler_execution(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    parent_project = tmp_path / "parent-project"
    parent_project.mkdir()
    (parent_project / "meridian.toml").write_text("", encoding="utf-8")
    parent_runtime = tmp_path / "parent-runtime"
    parent_runtime.mkdir()
    child_project = tmp_path / "child-project"
    child_project.mkdir()
    (child_project / "meridian.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", parent_project.as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", parent_runtime.as_posix())
    monkeypatch.chdir(child_project)
    captured: dict[str, tuple[str | None, str | None] | str] = {}

    monkeypatch.setattr(cli_main, "maybe_bootstrap_runtime_state", lambda *_a, **_k: child_project)

    def _capture_list(payload: ops_hooks.HookListInput) -> ops_hooks.HookListOutput:
        captured["env"] = (
            os.environ.get("MERIDIAN_PROJECT_DIR"),
            os.environ.get("MERIDIAN_RUNTIME_DIR"),
        )
        captured["project_root"] = payload.project_root or ""
        return ops_hooks.HookListOutput(hooks=())

    def _capture_run(payload: ops_hooks.HookRunInput) -> ops_hooks.HookRunOutput:
        captured["env"] = (
            os.environ.get("MERIDIAN_PROJECT_DIR"),
            os.environ.get("MERIDIAN_RUNTIME_DIR"),
        )
        captured["project_root"] = payload.project_root or ""
        return ops_hooks.HookRunOutput(
            hook=payload.name,
            event=payload.event or "spawn.finalized",
            result=ops_hooks.HookRunResult(
                outcome="success",
                success=True,
                skipped=False,
                duration_ms=1,
            ),
        )

    monkeypatch.setattr(hooks_cli, "hooks_list_sync", _capture_list)
    monkeypatch.setattr(hooks_cli, "hooks_run_sync", _capture_run)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(argv)

    assert exc_info.value.code == 0
    assert captured["env"] == (None, parent_runtime.as_posix())
    assert captured["project_root"] == child_project.resolve().as_posix()


def _write_hook_env_recorder(path: Path) -> None:
    path.write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "_ = json.loads(sys.stdin.read())\n"
        "target = Path(sys.argv[1])\n"
        "target.write_text(\n"
        "    json.dumps(\n"
        "        {\n"
        '            "MERIDIAN_RUNTIME_DIR": os.environ.get("MERIDIAN_RUNTIME_DIR"),\n'
        '            "MERIDIAN_PROJECT_DIR": os.environ.get("MERIDIAN_PROJECT_DIR"),\n'
        "        }\n"
        "    )\n"
        "    + '\\n',\n"
        "    encoding='utf-8',\n"
        ")\n",
        encoding="utf-8",
    )


def test_hook_subprocess_env_strips_stale_runtime_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    project_root = tmp_path / "hooks-env-project"
    project_root.mkdir()
    marker = tmp_path / "hook-env.json"
    recorder = tmp_path / "record_hook_env.py"
    _write_hook_env_recorder(recorder)
    command = _python_command(recorder, marker.as_posix())
    (project_root / "meridian.toml").write_text(
        f"[[hooks]]\nname = 'record-finalized'\nevent = 'spawn.finalized'\ncommand = '{command}'\n",
        encoding="utf-8",
    )
    stale_runtime = tmp_path / "stale-runtime"
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", stale_runtime.as_posix())
    monkeypatch.chdir(project_root)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["hooks", "run", "record-finalized"])

    assert exc_info.value.code == 0
    captured = json.loads(marker.read_text(encoding="utf-8"))
    assert captured["MERIDIAN_RUNTIME_DIR"] is None
    assert captured["MERIDIAN_PROJECT_DIR"] == project_root.resolve().as_posix()


def test_top_level_hooks_run_honors_runtime_override_in_hook_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    project_root = tmp_path / "hooks-run-project"
    project_root.mkdir()
    marker = tmp_path / "hook-events.jsonl"
    recorder = tmp_path / "record_hook.py"
    _write_hook_recorder(recorder)
    command = _python_command(recorder, marker.as_posix())
    (project_root / "meridian.toml").write_text(
        f"[[hooks]]\nname = 'record-finalized'\nevent = 'spawn.finalized'\ncommand = '{command}'\n",
        encoding="utf-8",
    )
    runtime_override = tmp_path / "custom-runtime"
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_override.as_posix())
    monkeypatch.chdir(project_root)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["hooks", "run", "record-finalized"])

    assert exc_info.value.code == 0
    payloads = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]
    assert len(payloads) == 1
    assert payloads[0]["runtime_root"] == runtime_override.resolve().as_posix()


def test_hooks_check_keeps_inherited_roots_during_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    inherited_project = (tmp_path / "parent-project").as_posix()
    inherited_runtime = (tmp_path / "parent-runtime").as_posix()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", inherited_project)
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", inherited_runtime)
    captured: dict[str, tuple[str | None, str | None]] = {}

    def _fake_bootstrap(
        _argv: list[str],
        *,
        render_mode: str,
        state_requirement: object,
    ) -> Path:
        _ = (render_mode, state_requirement)
        captured["env"] = (
            os.environ.get("MERIDIAN_PROJECT_DIR"),
            os.environ.get("MERIDIAN_RUNTIME_DIR"),
        )
        return tmp_path

    monkeypatch.setattr(cli_main, "maybe_bootstrap_runtime_state", _fake_bootstrap)
    monkeypatch.setattr(
        hooks_cli,
        "hooks_check_sync",
        lambda _payload: ops_hooks.HookCheckOutput(ok=True, checks=()),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["hooks", "check"])

    assert exc_info.value.code == 0
    assert captured["env"] == (inherited_project, inherited_runtime)


def test_hooks_check_exits_non_zero_when_requirements_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_hooks_check_sync(_payload: ops_hooks.HookCheckInput) -> ops_hooks.HookCheckOutput:
        return ops_hooks.HookCheckOutput(
            ok=False,
            checks=(
                ops_hooks.HookCheckItem(
                    name="git-autosync",
                    ok=False,
                    requirements=("git",),
                    error="git missing",
                ),
            ),
        )

    monkeypatch.setattr(hooks_cli, "hooks_check_sync", _fake_hooks_check_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["hooks", "check"])

    assert exc_info.value.code == 1
