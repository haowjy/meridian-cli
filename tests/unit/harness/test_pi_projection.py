# qa-validated: pi-rpc-quiescence
"""Pi projection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.pi import PiAdapter
from meridian.lib.harness.projections import pi_extension_projection
from meridian.lib.harness.projections.pi_extension_projection import PiExtensionLaunchProfile
from meridian.lib.harness.projections.project_pi_native_tui import (
    project_pi_native_tui_spec_to_cli_args,
)
from meridian.lib.harness.projections.project_pi_rpc import project_pi_spec_to_cli_args
from meridian.lib.launch.constants import BASE_COMMAND_PI_SUBPROCESS, PRIMARY_BASE_COMMAND_PI
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.user_paths import get_user_home


def _write_extension_fixture(root: Path) -> None:
    (root / "background-tasks").mkdir(parents=True, exist_ok=True)
    (root / "background-tasks" / "index.js").write_text("export default {}\n", encoding="utf-8")


def _bundle_path(root: Path, name: str) -> str:
    return str((root / name / "index.js").resolve())


def test_pi_rpc_projection_includes_rpc_resume_and_meridian_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))

    meridian_entrypoints = pi_extension_projection.resolve_pi_all_extension_entrypoints()
    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        model="anthropic/claude-sonnet-4",
        effort="high",
        prompt="solve this",
        continue_session_id="019e3113",
        continue_fork=True,
        appended_system_prompt="You are meridian worker",
        extra_args=("--provider", "anthropic"),
        pi_extension_entrypoints=meridian_entrypoints,
        load_all_pi_extensions=False,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    command = project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)

    assert command[0:3] == ["pi", "--mode", "rpc"]
    assert command.count("--mode") == 1
    assert command[command.index("--model") + 1] == "anthropic/claude-sonnet-4:high"
    assert command[command.index("--append-system-prompt") + 1] == "You are meridian worker"
    assert command[command.index("--fork") + 1] == "019e3113"
    assert command[command.index("--session-dir") + 1] == str(
        get_user_home() / "meridian-pi" / "sessions"
    )
    assert "--no-extensions" in command
    assert "--no-skills" in command
    assert "--no-context-files" in command
    assert "--no-prompt-templates" in command

    extension_values = [
        command[index + 1] for index, token in enumerate(command) if token == "-e"
    ]
    assert extension_values == [
        _bundle_path(extension_source_root, "background-tasks"),
    ]
    assert command[-2:] == ["--provider", "anthropic"]
    assert "solve this" not in command


def test_pi_rpc_projection_omits_no_extensions_when_load_all_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    user_extensions = tmp_path / "user" / "extensions"
    _write_extension_fixture(extension_source_root)
    (user_extensions / "custom-ext").mkdir(parents=True)
    (user_extensions / "custom-ext" / "index.js").write_text("export default {}\n")
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))

    meridian_entrypoints = pi_extension_projection.resolve_pi_extension_entrypoints(
        PiExtensionLaunchProfile(
            background_tasks_enabled=False,
            spawn_watch_enabled=True,
            interactive=False,
        )
    )
    extra_entrypoints = pi_extension_projection.resolve_extra_pi_extension_entrypoints(
        (user_extensions,)
    )
    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        pi_extension_entrypoints=meridian_entrypoints + extra_entrypoints,
        load_all_pi_extensions=True,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    command = project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)

    assert "--no-extensions" not in command
    extension_values = [
        command[index + 1] for index, token in enumerate(command) if token == "-e"
    ]
    assert _bundle_path(extension_source_root, "background-tasks") in extension_values
    assert str((user_extensions / "custom-ext" / "index.js").resolve()) in extension_values


def test_pi_rpc_projection_uses_session_without_fork_when_continue_fork_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))

    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        continue_session_id="abc1234",
        continue_fork=False,
        pi_extension_entrypoints=pi_extension_projection.resolve_pi_extension_entrypoints(
            PiExtensionLaunchProfile(
                background_tasks_enabled=False,
                spawn_watch_enabled=True,
                interactive=False,
            )
        ),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    command = project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)

    assert "--session" in command
    assert "--fork" not in command


def test_pi_rpc_projection_never_embeds_initial_prompt_in_cli_tail() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello over stdin",
        extra_args=("--foo", "bar"),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    command = project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)

    assert command[-2:] == ["--foo", "bar"]
    assert "hello over stdin" not in command


def test_pi_native_projection_loads_all_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))

    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        model="openai-codex/gpt-5.4-mini",
        effort="high",
        prompt="hello",
        continue_session_id="019e3113-edc8-7751-bb29-9648304465d5",
        continue_fork=True,
        appended_system_prompt="native primary",
        extra_args=("--provider", "openai"),
        interactive=True,
        pi_extension_entrypoints=pi_extension_projection.resolve_pi_all_extension_entrypoints(),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    command = project_pi_native_tui_spec_to_cli_args(spec, base_command=PRIMARY_BASE_COMMAND_PI)

    assert command[0] == "pi"
    assert "--mode" not in command
    assert "--no-extensions" not in command
    extension_values = [
        command[index + 1] for index, token in enumerate(command) if token == "-e"
    ]
    assert extension_values == [
        _bundle_path(extension_source_root, "background-tasks"),
    ]
    assert command[command.index("--model") + 1] == "openai-codex/gpt-5.4-mini:high"
    assert command[command.index("--append-system-prompt") + 1] == "native primary"
    assert command[command.index("--fork") + 1] == "019e3113-edc8-7751-bb29-9648304465d5"
    assert command[-2:] == ["--provider", "openai"]


def test_pi_extension_projection_non_interactive_loads_background_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))

    entrypoints = pi_extension_projection.resolve_pi_extension_entrypoints(
        PiExtensionLaunchProfile(
            background_tasks_enabled=True,
            spawn_watch_enabled=True,
            interactive=False,
        )
    )

    assert entrypoints == (_bundle_path(extension_source_root, "background-tasks"),)


def test_pi_extension_projection_spawn_watch_alias_loads_background_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))

    entrypoints = pi_extension_projection.resolve_pi_extension_entrypoints(
        PiExtensionLaunchProfile(
            background_tasks_enabled=False,
            spawn_watch_enabled=True,
            interactive=True,
        )
    )

    assert entrypoints == (_bundle_path(extension_source_root, "background-tasks"),)


def test_pi_all_extension_entrypoints_keeps_interactive_managed_bash_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))

    entrypoints = pi_extension_projection.resolve_pi_all_extension_entrypoints()

    assert entrypoints == (_bundle_path(extension_source_root, "background-tasks"),)


def test_pi_native_projection_uses_session_without_fork_when_continue_fork_false() -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        continue_session_id="abc1234",
        continue_fork=False,
        interactive=True,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    command = project_pi_native_tui_spec_to_cli_args(spec, base_command=PRIMARY_BASE_COMMAND_PI)

    assert command[command.index("--session") + 1] == "abc1234"
    assert "--fork" not in command
    assert "--mode" not in command


@pytest.mark.parametrize(
    ("extra_args", "message", "continue_fork"),
    [
        (("--session", "user-session"), "cannot accept --session", False),
        (("--fork", "user-session"), "cannot accept --fork", True),
        (("--mode", "rpc"), "cannot accept --mode", False),
        (("--session-dir", "/tmp/user-session-dir"), "cannot accept --session-dir", False),
        (("--no-extensions",), "cannot accept --no-extensions", False),
        (("-e", "/tmp/custom-extension.js"), "-e/--extension", False),
    ],
)
def test_pi_native_projection_rejects_owned_cli_surface(
    extra_args: tuple[str, ...],
    message: str,
    continue_fork: bool,
) -> None:
    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        interactive=True,
        continue_session_id="managed-session",
        continue_fork=continue_fork,
        extra_args=extra_args,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(ValueError, match=message):
        project_pi_native_tui_spec_to_cli_args(spec, base_command=PRIMARY_BASE_COMMAND_PI)


def test_pi_adapter_resolve_launch_spec_uses_all_extensions_for_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.delenv("MERIDIAN_PI_DISABLE_MANAGED_BASH", raising=False)
    monkeypatch.delenv("MERIDIAN_PI_MANAGED_BASH", raising=False)

    adapter = PiAdapter()

    spec = adapter.resolve_launch_spec(
        SpawnParams(prompt="primary should run", interactive=True),
        UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    assert spec.pi_extension_entrypoints == (
        _bundle_path(extension_source_root, "background-tasks"),
    )
    assert spec.load_all_pi_extensions is False


def test_pi_adapter_resolve_launch_spec_uses_background_tasks_for_spawned_non_interactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.delenv("MERIDIAN_PI_DISABLE_MANAGED_BASH", raising=False)
    monkeypatch.delenv("MERIDIAN_PI_MANAGED_BASH", raising=False)

    adapter = PiAdapter()

    spec = adapter.resolve_launch_spec(
        SpawnParams(prompt="spawned should run", interactive=False),
        UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    assert spec.pi_extension_entrypoints == (
        _bundle_path(extension_source_root, "background-tasks"),
    )


@pytest.mark.parametrize(
    ("extra_args", "message"),
    [
        (("--mode", "json"), "owns --mode"),
        (("--session-dir", "/tmp/user-session-dir"), "owns --session-dir"),
        (("--no-extensions",), "extension loading"),
        (("-e", "/tmp/custom-extension.js"), "-e/--extension"),
        (("--extension", "/tmp/custom-extension.js"), "-e/--extension"),
    ],
)
def test_pi_rpc_projection_rejects_owned_cli_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: tuple[str, ...],
    message: str,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))

    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        extra_args=extra_args,
        pi_extension_entrypoints=pi_extension_projection.resolve_pi_all_extension_entrypoints(),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(ValueError, match=message):
        project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)


def test_pi_extension_projection_fails_when_required_entrypoint_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    extension_source_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_INSTALL_ROOT", str(tmp_path / "empty-install"))

    with pytest.raises(pi_extension_projection.PiExtensionProjectionError) as exc_info:
        pi_extension_projection.resolve_pi_all_extension_entrypoints()

    message = str(exc_info.value)
    assert "background-tasks" in message and "index.js" in message
    assert "Build Pi extensions first" in message
