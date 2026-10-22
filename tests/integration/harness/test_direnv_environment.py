"""Exercise direnv's real reversal protocol with isolated, non-secret fixtures."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.bundle import project_subprocess_spec
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.opencode_connection import OpenCodeConnection
from meridian.lib.harness.opencode import project_opencode_primary_preview
from meridian.lib.harness.pi import PiAdapter
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.env import build_harness_child_env, undo_direnv
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.plan import build_primary_launch_runtime, build_primary_spawn_request
from meridian.lib.launch.types import LaunchRequest
from tests.support.launch import build_launch_context, stub_bundle_request_and_resolve


def test_undo_direnv_without_metadata_needs_no_executable() -> None:
    assert undo_direnv({"PATH": "", "KEEP": "value"}) == {"PATH": "", "KEEP": "value"}


def test_undo_direnv_missing_executable_refuses_inheritance() -> None:
    with pytest.raises(ValueError, match="direnv is not on PATH"):
        undo_direnv({"PATH": "", "DIRENV_DIFF": "sensitive-diff"})


@pytest.fixture
def direnv_environments(tmp_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    executable = shutil.which("direnv")
    if executable is None:
        pytest.skip("direnv is not installed")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".envrc").write_text(
        "export ANTHROPIC_API_KEY=fake-test-key\n"
        "export CHANGED=after\nunset REMOVED\n"
        "export PATH=/direnv-only:$PATH\n"
        "export MERIDIAN_IGNORE_DIRENV=true\n"
        "export PI_CODING_AGENT_DIR=/direnv-auth\n"
        "export PI_CODING_AGENT_SESSION_DIR=/direnv-sessions\n"
        "export HOME=/direnv-home\n"
        "export MERIDIAN_HOME=/direnv-meridian\n"
        "export OPENCODE_CONFIG_CONTENT='"
        '{"provider":{"anthropic":{"options":{"apiKey":"fake-test-key"}}}}'
        "'\n"
    )
    base = {
        "HOME": str(tmp_path),
        "PATH": os.pathsep.join((str(Path(executable).parent), os.defpath)),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "CHANGED": "before",
        "REMOVED": "original",
        "OPENCODE_CONFIG_CONTENT": '{"permission":{"bash":"ask"}}',
    }
    subprocess.run([executable, "allow", str(project)], env=base, check=True, capture_output=True)
    active = json.loads(subprocess.check_output(
        [executable, "exec", str(project), executable, "dump", "json"], env=base,
    ))
    return base, active


def test_real_direnv_restores_environment(
    direnv_environments: tuple[dict[str, str], dict[str, str]],
) -> None:
    base, active = direnv_environments
    assert active["ANTHROPIC_API_KEY"] == "fake-test-key"
    restored = undo_direnv(active)
    assert "ANTHROPIC_API_KEY" not in restored
    assert "DIRENV_DIFF" not in restored
    assert restored["CHANGED"] == "before"
    assert restored["REMOVED"] == "original"
    assert restored["PATH"] == base["PATH"]
    assert undo_direnv(restored) == restored
    with pytest.raises(ValueError, match="environment restoration failed") as failure:
        undo_direnv({**base, "DIRENV_DIFF": "sensitive-invalid-diff"})
    assert "sensitive" not in str(failure.value)


def test_pi_defaults_use_restored_home_and_paths(
    direnv_environments: tuple[dict[str, str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, active = direnv_environments
    for key, value in active.items():
        monkeypatch.setenv(key, value)
    env = build_harness_child_env(
        base_env=undo_direnv(active), adapter=PiAdapter(),
        run_params=SpawnParams(prompt="test"),
    )
    assert env["PI_CODING_AGENT_DIR"] == str(Path(base["HOME"]) / ".pi" / "agent")
    assert env["PI_CODING_AGENT_SESSION_DIR"] == str(
        Path(base["HOME"]) / ".meridian" / "meridian-pi" / "sessions"
    )
    explicit = build_harness_child_env(
        base_env=undo_direnv(active), adapter=PiAdapter(),
        run_params=SpawnParams(prompt="test"),
        runtime_env_overrides={"MERIDIAN_HOME": "~/explicit-state"},
    )
    assert explicit["PI_CODING_AGENT_SESSION_DIR"] == str(
        Path(base["HOME"]) / "explicit-state" / "meridian-pi" / "sessions"
    )


@pytest.mark.parametrize("ignore_direnv", [None, True, False])
def test_opencode_workspace_projection_uses_effective_environment(
    tmp_path: Path,
    direnv_environments: tuple[dict[str, str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    ignore_direnv: bool | None,
) -> None:
    base, active = direnv_environments
    # Keep Meridian's control/state roots test-local; dirty provider JSON stays ambient.
    for key, value in active.items():
        if key not in {"HOME", "MERIDIAN_HOME"}:
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOME", base["HOME"])
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".opencode"]\n')
    stub_bundle_request_and_resolve(
        monkeypatch, model="anthropic/claude-sonnet-4", harness=HarnessId.OPENCODE,
    )
    context = build_launch_context(
        spawn_id="dry-run-direnv",
        request=build_primary_spawn_request(request=LaunchRequest(
            model="anthropic/claude-sonnet-4", harness="opencode",
            ignore_direnv=ignore_direnv,
        )),
        runtime=build_primary_launch_runtime(project_root=tmp_path),
        harness_registry=get_default_harness_registry(), dry_run=True,
    )
    env = context.binding.environment.final_env
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    expected = ignore_direnv is not False
    assert ("provider" not in config) is expected
    assert ("ANTHROPIC_API_KEY" not in env) is expected
    assert env["MERIDIAN_IGNORE_DIRENV"] == str(expected).lower()
    agent_key = "agent" if context.binding.spec.opencode_version == "v1" else "agents"
    permission_key = (
        "permission" if context.binding.spec.opencode_version == "v1" else "permissions"
    )
    agent_permissions = config[agent_key]["meridian"][permission_key]
    if context.binding.spec.opencode_version == "v1":
        assert agent_permissions["external_directory"]
    else:
        assert any(rule["action"] == "external_directory" for rule in agent_permissions)
    if expected:
        assert config["permission"]["bash"] == "ask"


@pytest.mark.parametrize("surface", ["connection", "preview", "subprocess"])
@pytest.mark.parametrize("version", ["v1", "v2"])
def test_opencode_probes_use_bound_path_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str, version: str,
) -> None:
    bound_bin = tmp_path / "bound"
    ambient_bin = tmp_path / "ambient"
    bound_bin.mkdir()
    ambient_bin.mkdir()
    marker = tmp_path / "probe"
    for directory, result in ((bound_bin, "bound"), (ambient_bin, "ambient")):
        executable = directory / "opencode"
        version_output = "1.18.0" if version == "v1" else "2.0.6"
        executable.write_text(
            '#!/bin/sh\nprintf "%s:%s" ' + result
            + ' "${ANTHROPIC_API_KEY-unset}" > "' + str(marker)
            + '"\necho ' + version_output + '\n'
        )
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(ambient_bin))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-ambient-key")
    env = {"PATH": str(bound_bin), "MERIDIAN_HARNESS_OPENCODE_VERSION": "auto"}
    spec = ResolvedLaunchSpec(
        harness=HarnessId.OPENCODE,
        model="anthropic/claude-sonnet-4", continue_session_id="session-review",
    )
    if surface == "connection":
        config = ConnectionConfig(
            spawn_id=SpawnId("p-review"), harness_id=HarnessId.OPENCODE,
            prompt="", control_root=tmp_path, child_env=env,
        )
        assert OpenCodeConnection()._resolve_version(config) == version
    elif surface == "preview":
        project_opencode_primary_preview(spec, project_root=tmp_path, env=env)
    elif version == "v1":
        with pytest.raises(HarnessCapabilityMismatch, match="cannot switch the model"):
            project_subprocess_spec(
                HarnessId.OPENCODE, spec, base_command=("opencode", "run"), env=env,
            )
    else:
        project_subprocess_spec(
            HarnessId.OPENCODE, spec, base_command=("opencode", "run"), env=env,
        )
    assert marker.read_text() == "bound:unset"
