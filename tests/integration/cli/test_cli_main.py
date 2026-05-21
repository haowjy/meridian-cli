# qa-validated: pi-rpc-quiescence
import importlib
import os
import shlex
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.core.types import HarnessId
from tests.support.fixtures import write_minimal_mars_config
from tests.support.launch import stub_bundle_request_and_resolve
from tests.support.pi_extensions import configure_pi_extension_projection

cli_main = importlib.import_module("meridian.cli.main")
primary_launch = importlib.import_module("meridian.cli.primary_launch")
init_ops = importlib.import_module("meridian.lib.ops.init_ops")
bootstrap_services = importlib.import_module("meridian.lib.bootstrap.services")
cli_utils = importlib.import_module("meridian.cli.utils")


def test_main_harness_shortcut_routes_into_primary_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_primary_launch(**kwargs: object) -> object:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["codex", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["harness"] == "codex"
    assert captured["dry_run"] is True


def test_main_skips_fork_normalization_for_mars_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_run_mars_passthrough(
        args: list[str],
        *,
        output_format: str | None = None,
    ) -> None:
        captured["args"] = tuple(args)
        captured["output_format"] = output_format
        raise SystemExit(0)

    monkeypatch.setattr(cli_main, "_run_mars_passthrough", _fake_run_mars_passthrough)
    monkeypatch.setattr(cli_main, "_emit_usage_command_invoked", lambda *_a, **_k: None)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["mars", "models", "list", "--fork"])

    assert exc_info.value.code == 0
    assert captured["args"] == ("models", "list", "--fork")
    assert captured["output_format"] == "text"


def test_main_bare_fork_is_normalized_before_primary_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p123")
    captured: dict[str, object] = {}

    def _fake_primary_launch(**kwargs: object) -> object:
        captured.update(kwargs)
        return primary_launch.PrimaryLaunchOutput(message="ok", exit_code=0)

    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--fork", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["fork_ref"] == "__SELF__"
    assert captured["fork_fresh_ref"] is None


def test_main_from_forwards_to_primary_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_primary_launch(**kwargs: object) -> object:
        captured.update(kwargs)
        return primary_launch.PrimaryLaunchOutput(message="ok", exit_code=0)

    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--from", "p123", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["from_ref"] == "p123"
    assert captured["fork_ref"] is None
    assert captured["fork_fresh_ref"] is None


def test_main_bare_from_is_normalized_before_primary_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p123")
    captured: dict[str, object] = {}

    def _fake_primary_launch(**kwargs: object) -> object:
        captured.update(kwargs)
        return primary_launch.PrimaryLaunchOutput(message="ok", exit_code=0)

    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--from", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["from_ref"] == "__SELF__"


def test_main_bare_from_without_meridian_spawn_id_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "--from", "--dry-run"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "error: Cannot infer --from target: not inside a Meridian-managed session. "
        "Pass --from REF explicitly.\n"
    )


def test_main_bare_fork_without_meridian_spawn_id_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "--fork", "--dry-run"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "error: Cannot infer --fork target: not inside a Meridian-managed session. "
        "Pass --fork REF explicitly.\n"
    )


def test_main_fork_rejects_model_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "--fork", "c123", "-m", "gpt-5.4-mini", "--dry-run"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "error: --fork preserves launch identity. "
        "Use --fork-fresh to change agent, model, or skills.\n"
    )


def test_main_fork_fresh_allows_model_and_agent_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_primary_launch(**kwargs: object) -> object:
        captured.update(kwargs)
        return primary_launch.PrimaryLaunchOutput(message="ok", exit_code=0)

    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            [
                "--fork-fresh",
                "c123",
                "-m",
                "gpt-5.4-mini",
                "-a",
                "reviewer",
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 0
    assert captured["fork_ref"] is None
    assert captured["fork_fresh_ref"] == "c123"
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["agent"] == "reviewer"


def test_main_pi_primary_launch_dry_run_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    write_minimal_mars_config(tmp_path)
    configure_pi_extension_projection(monkeypatch, tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4-mini",
        harness=HarnessId.PI,
    )
    monkeypatch.setattr(
        cli_main,
        "maybe_bootstrap_runtime_state",
        lambda *_args, **_kwargs: tmp_path,
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--harness", "pi", "--model", "gpt-5.4-mini", "--dry-run"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "pi" in captured.out
    command_start = captured.out.find("pi ")
    assert command_start >= 0
    dry_run_argv = shlex.split(captured.out[command_start:])
    assert "--mode" not in dry_run_argv
    assert "--mode rpc" not in captured.out


def test_init_alias_link_uses_mars_flow_with_full_link_target_when_called_directly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_run_init_flow(
        *,
        project_root: Path,
        add_sources: list[str],
        link_targets: list[str] | None = None,
        output_format: str = "text",
    ) -> object:
        captured.update(
            {
                "project_root": project_root.as_posix(),
                "add_sources": add_sources,
                "link_targets": link_targets,
                "output_format": output_format,
            }
        )
        return object()

    monkeypatch.setattr(init_ops, "run_init_flow", _fake_run_init_flow)
    monkeypatch.setattr(cli_main, "emit", lambda _payload: None)

    cli_main.init_alias(path=tmp_path.as_posix(), link=[".claude"])

    expected_root = tmp_path.resolve().as_posix()
    assert captured == {
        "project_root": expected_root,
        "add_sources": [],
        "link_targets": [".claude"],
        "output_format": "text",
    }


def test_bootstrap_command_enables_bootstrap_documents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "maybe_bootstrap_runtime_state", lambda *_args, **_kwargs: None)

    def _fake_primary_launch(**kwargs: object) -> object:
        captured.update(kwargs)
        return primary_launch.PrimaryLaunchOutput(message="ok", exit_code=0)

    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["bootstrap", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["include_bootstrap_documents"] is True


def test_bootstrap_with_setup_runs_init_flow_and_passes_project_root_to_primary_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_init: dict[str, object] = {}
    captured_launch: dict[str, object] = {}
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    nested_cwd = repo_root / "nested" / "project"
    nested_cwd.mkdir(parents=True)
    monkeypatch.chdir(nested_cwd)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)
    monkeypatch.setattr(
        cli_utils,
        "require_established_project_root",
        lambda: pytest.fail("unexpected startup root resolution"),
    )
    monkeypatch.setattr(
        bootstrap_services,
        "prepare_for_runtime_write",
        lambda _project_root: pytest.fail("unexpected startup runtime bootstrap"),
    )

    def _fake_run_init_flow(
        *,
        project_root: Path,
        add_sources: list[str],
        link_targets: list[str] | None = None,
        output_format: str = "text",
    ) -> object:
        captured_init.update(
            {
                "project_root": project_root.as_posix(),
                "add_sources": add_sources,
                "link_targets": link_targets,
                "output_format": output_format,
            }
        )
        return object()

    def _fake_primary_launch(**kwargs: object) -> object:
        captured_launch.update(kwargs)
        return primary_launch.PrimaryLaunchOutput(message="ok", exit_code=0)

    monkeypatch.setattr(init_ops, "run_init_flow", _fake_run_init_flow)
    monkeypatch.setattr(primary_launch, "run_primary_launch", _fake_primary_launch)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            [
                "bootstrap",
                "--add",
                "haowjy/meridian-dev-workflow",
                "--add",
                "haowjy/meridian-base",
                "--link",
                ".claude",
                "--link",
                ".cursor",
            ]
        )

    assert exc_info.value.code == 0
    expected_root = nested_cwd.resolve().as_posix()
    assert captured_init == {
        "project_root": expected_root,
        "add_sources": ["haowjy/meridian-dev-workflow", "haowjy/meridian-base"],
        "link_targets": [".claude", ".cursor"],
        "output_format": "text",
    }
    assert captured_launch["project_root"] == nested_cwd.resolve()
    assert captured_launch["include_bootstrap_documents"] is True


def test_bootstrap_setup_flags_reject_dry_run_before_setup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo-root"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    nested_cwd = repo_root / "nested" / "project"
    nested_cwd.mkdir(parents=True)
    monkeypatch.chdir(nested_cwd)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)
    monkeypatch.setattr(
        cli_utils,
        "require_established_project_root",
        lambda: pytest.fail("unexpected startup root resolution"),
    )
    monkeypatch.setattr(
        bootstrap_services,
        "prepare_for_runtime_write",
        lambda _project_root: pytest.fail("unexpected startup runtime bootstrap"),
    )
    monkeypatch.setattr(init_ops, "run_init_flow", lambda **_kwargs: pytest.fail("unexpected init"))
    monkeypatch.setattr(
        primary_launch,
        "run_primary_launch",
        lambda **_kwargs: pytest.fail("unexpected primary launch"),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["bootstrap", "--add", "haowjy/meridian-dev-workflow", "--dry-run"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: Cannot combine setup flags (--add/--link) with --dry-run.\n"


def test_workspace_unknown_subcommand_help_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["workspace", "migrate", "--help"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: Unknown command: workspace migrate\n"


@pytest.mark.parametrize(
    ("argv", "nested", "expects_override"),
    [
        (["hooks"], True, True),
        (["hooks"], False, False),
        (["hooks", "check"], True, False),
    ],
)
def test_hooks_bootstrap_authority_override_applies_only_to_manual_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    nested: bool,
    expects_override: bool,
) -> None:
    parent_project_dir = str((tmp_path / "parent-project").resolve())
    parent_runtime_dir = str((tmp_path / "parent-runtime").resolve())
    if nested:
        monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    else:
        monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", parent_project_dir)
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", parent_runtime_dir)
    captured_env: list[tuple[str | None, str | None]] = []

    def _fake_bootstrap(*_args: object, **_kwargs: object) -> Path | None:
        captured_env.append(
            (
                os.environ.get("MERIDIAN_PROJECT_DIR"),
                os.environ.get("MERIDIAN_RUNTIME_DIR"),
            )
        )
        return None

    monkeypatch.setattr(cli_main, "maybe_bootstrap_runtime_state", _fake_bootstrap)
    monkeypatch.setattr(cli_main, "_register_commands_for_invocation", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cli_main,
        "_emit_usage_command_invoked",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(cli_main, "app", lambda _argv: None)

    cli_main.main(argv)

    assert captured_env == [
        (
            None if expects_override else parent_project_dir,
            None if expects_override else parent_runtime_dir,
        )
    ]
    assert os.environ.get("MERIDIAN_PROJECT_DIR") == parent_project_dir
    assert os.environ.get("MERIDIAN_RUNTIME_DIR") == parent_runtime_dir
