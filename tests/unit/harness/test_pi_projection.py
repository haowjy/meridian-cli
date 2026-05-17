# qa-validated: pi-rpc-quiescence
"""Pi RPC projection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections import pi_extension_projection
from meridian.lib.harness.projections.project_pi_rpc import project_pi_spec_to_cli_args
from meridian.lib.launch.constants import BASE_COMMAND_PI_SUBPROCESS
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def _write_extension_fixture(root: Path) -> None:
    (root / "managed-bash").mkdir(parents=True, exist_ok=True)
    (root / "managed-bash" / "index.js").write_text("export default {}\n", encoding="utf-8")
    (root / "meridian-lifecycle").mkdir(parents=True, exist_ok=True)
    (root / "meridian-lifecycle" / "index.js").write_text(
        "export default {}\n",
        encoding="utf-8",
    )


def test_pi_rpc_projection_includes_rpc_resume_and_meridian_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    extension_target_root = tmp_path / "agent" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(extension_target_root))

    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        model="anthropic/claude-sonnet-4",
        effort="high",
        prompt="solve this",
        continue_session_id="019e3113",
        continue_fork=True,
        appended_system_prompt="You are meridian worker",
        extra_args=("--provider", "anthropic"),
        pi_extension_entrypoints=pi_extension_projection.resolve_pi_extension_entrypoints(),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    command = project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)

    assert command[0:3] == ["meridian-pi", "--mode", "rpc"]
    assert command.count("--mode") == 1
    assert command[command.index("--model") + 1] == "anthropic/claude-sonnet-4:high"
    assert command[command.index("--append-system-prompt") + 1] == "You are meridian worker"
    assert command[command.index("--fork") + 1] == "019e3113"
    assert "--no-extensions" in command
    assert "--no-skills" in command
    assert "--no-context-files" in command
    assert "--no-prompt-templates" in command

    extension_values = [
        command[index + 1] for index, token in enumerate(command) if token == "-e"
    ]
    assert extension_values == [
        str(extension_target_root / "managed-bash" / "index.js"),
        str(extension_target_root / "meridian-lifecycle" / "index.js"),
    ]
    assert command[-2:] == ["--provider", "anthropic"]
    assert "solve this" not in command


def test_pi_rpc_projection_uses_session_without_fork_when_continue_fork_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    extension_target_root = tmp_path / "agent" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(extension_target_root))

    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        continue_session_id="abc1234",
        continue_fork=False,
        pi_extension_entrypoints=pi_extension_projection.resolve_pi_extension_entrypoints(),
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


def test_pi_rpc_projection_rejects_user_mode_passthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    extension_target_root = tmp_path / "agent" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(extension_target_root))

    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        extra_args=("--mode", "json"),
        pi_extension_entrypoints=pi_extension_projection.resolve_pi_extension_entrypoints(),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(ValueError, match="owns --mode"):
        project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)


@pytest.mark.parametrize(
    ("extra_args", "message"),
    [
        (("--no-extensions",), "extension loading"),
        (("-e", "/tmp/custom-extension.js"), "-e/--extension"),
        (("--extension", "/tmp/custom-extension.js"), "-e/--extension"),
    ],
)
def test_pi_rpc_projection_rejects_passthrough_extension_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: tuple[str, ...],
    message: str,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    extension_target_root = tmp_path / "agent" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(extension_target_root))

    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        extra_args=extra_args,
        pi_extension_entrypoints=pi_extension_projection.resolve_pi_extension_entrypoints(),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    with pytest.raises(ValueError, match=message):
        project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)


def test_pi_extension_projection_uses_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    extension_target_root = tmp_path / "agent" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(extension_target_root))

    managed_target = extension_target_root / "managed-bash" / "index.js"
    lifecycle_target = extension_target_root / "meridian-lifecycle" / "index.js"
    managed_target.parent.mkdir(parents=True, exist_ok=True)
    lifecycle_target.parent.mkdir(parents=True, exist_ok=True)
    managed_target.write_text("stale managed\n", encoding="utf-8")
    lifecycle_target.write_text("stale lifecycle\n", encoding="utf-8")

    entrypoints = pi_extension_projection.resolve_pi_extension_entrypoints()

    assert entrypoints == (str(managed_target), str(lifecycle_target))
    assert managed_target.read_text(encoding="utf-8") == "export default {}\n"
    assert lifecycle_target.read_text(encoding="utf-8") == "export default {}\n"
    assert list(managed_target.parent.glob("*.tmp-*")) == []
    assert list(lifecycle_target.parent.glob("*.tmp-*")) == []


def test_pi_extension_projection_fails_when_required_entrypoint_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    extension_target_root = tmp_path / "agent" / "extensions"
    (extension_source_root / "managed-bash").mkdir(parents=True, exist_ok=True)
    (extension_source_root / "managed-bash" / "index.js").write_text(
        "export default {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(extension_target_root))

    with pytest.raises(pi_extension_projection.PiExtensionProjectionError) as exc_info:
        pi_extension_projection.resolve_pi_extension_entrypoints()

    message = str(exc_info.value)
    assert "meridian-lifecycle/index.js" in message
    assert "Build runtime extensions first" in message


def test_pi_extension_projection_default_target_is_launch_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.delenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", raising=False)
    monkeypatch.setattr(pi_extension_projection, "get_user_home", lambda: tmp_path / "user-home")

    entrypoints = pi_extension_projection.resolve_pi_extension_entrypoints()

    shared_root = tmp_path / "user-home" / "meridian-pi" / "agent" / "extensions"
    expected_fixed_paths = (
        str(shared_root / "managed-bash" / "index.js"),
        str(shared_root / "meridian-lifecycle" / "index.js"),
    )
    assert entrypoints != expected_fixed_paths

    launch_root = Path(entrypoints[0]).parents[1]
    assert launch_root.parent == shared_root
    assert launch_root != shared_root
    assert Path(entrypoints[1]).parents[1] == launch_root


def test_pi_extension_projection_default_target_is_unique_per_projection_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.delenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", raising=False)
    monkeypatch.setattr(pi_extension_projection, "get_user_home", lambda: tmp_path / "user-home")

    first = pi_extension_projection.resolve_pi_extension_entrypoints()
    second = pi_extension_projection.resolve_pi_extension_entrypoints()

    assert first != second
    assert Path(first[0]).parents[1] != Path(second[0]).parents[1]
    assert Path(first[1]).parents[1] == Path(first[0]).parents[1]
    assert Path(second[1]).parents[1] == Path(second[0]).parents[1]


def test_pi_rpc_projection_is_pure_with_precomputed_extension_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension_source_root = tmp_path / "dist" / "extensions"
    _write_extension_fixture(extension_source_root)
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(extension_source_root))
    monkeypatch.delenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", raising=False)
    monkeypatch.setattr(pi_extension_projection, "get_user_home", lambda: tmp_path / "user-home")

    entrypoints = pi_extension_projection.resolve_pi_extension_entrypoints()
    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt="hello",
        pi_extension_entrypoints=entrypoints,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    first = project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)
    second = project_pi_spec_to_cli_args(spec, base_command=BASE_COMMAND_PI_SUBPROCESS)

    assert first == second
    launch_roots = {Path(path).parents[1] for path in entrypoints}
    assert len(launch_roots) == 1
    extension_root = tmp_path / "user-home" / "meridian-pi" / "agent" / "extensions"
    launch_dirs = [path for path in extension_root.iterdir() if path.is_dir()]
    assert len(launch_dirs) == 1
