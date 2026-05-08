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


def test_hooks_group_is_registered() -> None:
    assert "hooks" in cli_main.app.resolved_commands()


def test_hooks_list_routes_through_hooks_ops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "hooks-list-project"
    project_root.mkdir(parents=True)
    (project_root / "meridian.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    captured: dict[str, object] = {}

    def _fake_hooks_list_sync(payload: ops_hooks.HookListInput) -> ops_hooks.HookListOutput:
        captured["payload"] = payload
        return ops_hooks.HookListOutput(hooks=())

    monkeypatch.setattr(hooks_cli, "hooks_list_sync", _fake_hooks_list_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["hooks", "list"])

    assert exc_info.value.code == 0
    assert isinstance(captured["payload"], ops_hooks.HookListInput)
    assert captured["payload"].project_root == project_root.resolve().as_posix()


def test_hooks_run_passes_hook_name_event_and_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    project_root = tmp_path / "hooks-run-project"
    project_root.mkdir(parents=True)
    (project_root / "meridian.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    captured: dict[str, object] = {}

    def _fake_hooks_run_sync(payload: ops_hooks.HookRunInput) -> ops_hooks.HookRunOutput:
        captured["payload"] = payload
        return ops_hooks.HookRunOutput(
            hook=payload.name,
            event="spawn.finalized",
            result=ops_hooks.HookRunResult(
                outcome="success",
                success=True,
                skipped=False,
                duration_ms=1,
            ),
        )

    monkeypatch.setattr(hooks_cli, "hooks_run_sync", _fake_hooks_run_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["hooks", "run", "record-finalized", "--event", "work.done"])

    assert exc_info.value.code == 0
    assert isinstance(captured["payload"], ops_hooks.HookRunInput)
    assert captured["payload"].name == "record-finalized"
    assert captured["payload"].event == "work.done"
    assert captured["payload"].project_root == project_root.resolve().as_posix()


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
        ["hooks", "list"],
        ["hooks", "run", "record-finalized"],
    ],
)
def test_hooks_list_and_run_ignore_inherited_roots_during_bootstrap(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", (tmp_path / "parent-project").as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", (tmp_path / "parent-runtime").as_posix())
    project_root = tmp_path / "child-project"
    project_root.mkdir()
    (project_root / "meridian.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(project_root)
    captured: dict[str, tuple[str | None, str | None]] = {}

    def _fake_bootstrap(
        _argv: list[str],
        *,
        agent_mode: bool,
        state_requirement: object,
    ) -> Path:
        _ = (agent_mode, state_requirement)
        captured["env"] = (
            os.environ.get("MERIDIAN_PROJECT_DIR"),
            os.environ.get("MERIDIAN_RUNTIME_DIR"),
        )
        return project_root

    monkeypatch.setattr(cli_main, "maybe_bootstrap_runtime_state", _fake_bootstrap)
    monkeypatch.setattr(
        hooks_cli,
        "hooks_list_sync",
        lambda _payload: ops_hooks.HookListOutput(hooks=()),
    )
    monkeypatch.setattr(
        hooks_cli,
        "hooks_run_sync",
        lambda payload: ops_hooks.HookRunOutput(
            hook=payload.name,
            event=payload.event or "spawn.finalized",
            result=ops_hooks.HookRunResult(
                outcome="success",
                success=True,
                skipped=False,
                duration_ms=1,
            ),
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(argv)

    assert exc_info.value.code == 0
    assert captured["env"] == (None, None)


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
        agent_mode: bool,
        state_requirement: object,
    ) -> Path:
        _ = (agent_mode, state_requirement)
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
