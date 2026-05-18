# qa-validated: pi-rpc-quiescence
"""Pi native primary launch runner boundary tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.pi_runtime_resolver import PiRuntimeResolution
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.process.runner import run_harness_process
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.state.primary_meta import read_primary_metadata


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


def _build_pi_primary_launch_context(project_root: Path) -> tuple[Any, Any]:
    _write_minimal_mars_config(project_root)
    harness_registry = get_default_harness_registry()
    config = load_config(project_root)
    launch_context = build_launch_context(
        spawn_id="dry-run-primary-pi",
        request=SpawnRequest(
            prompt="primary prompt",
            prompt_is_composed=False,
            model="openai-codex/gpt-5.4-mini",
            harness=HarnessId.PI.value,
            session=SessionRequest(),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            config_snapshot=config.model_dump(mode="json", exclude_none=True),
            runtime_root=(project_root / ".meridian").as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=project_root.as_posix(),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )
    return launch_context, harness_registry


@pytest.mark.slow
def test_run_harness_process_pi_primary_persists_native_metadata_at_runner_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary"
    project_root.mkdir()
    launch_context, harness_registry = _build_pi_primary_launch_context(project_root)
    pi_adapter = harness_registry.get_subprocess_harness(HarnessId.PI)
    monkeypatch.setattr(
        pi_adapter,
        "observe_session_id",
        lambda **_kwargs: "ses-native-observed",
    )
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 4.5.6",
        ),
    )

    captured: dict[str, object] = {}

    def fake_run_primary_process_with_capture(
        command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        captured["output_log_path"] = output_log_path
        assert callable(on_child_started)
        on_child_started(777)
        return (0, 777)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert outcome.primary_spawn_id is not None
    command = captured["command"]
    assert isinstance(command, tuple)
    assert command[0] == "/usr/local/bin/pi"
    assert "--mode" not in command
    assert "--no-extensions" not in command
    assert "--no-skills" not in command
    assert "--no-context-files" not in command
    assert "--no-prompt-templates" not in command
    assert "-e" not in command
    assert "--extension" not in command
    assert captured["output_log_path"] is None
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["MERIDIAN_PI_BINARY"] == "/usr/local/bin/pi"
    assert env["MERIDIAN_PI_SESSION_ROLE"] == "primary"
    assert env["PI_CODING_AGENT_SESSION_DIR"].endswith("meridian-pi/sessions")
    assert "PI_CODING_AGENT_DIR" not in env
    assert "MERIDIAN_PI_WRAPPER_METADATA_PATH" not in env

    metadata = read_primary_metadata(launch_context.runtime_root, str(outcome.primary_spawn_id))
    assert metadata is not None
    assert metadata.managed_backend is False
    assert metadata.command == tuple(command)
    assert metadata.launch_cwd == str(project_root)
    assert metadata.started_at_epoch is not None
    assert metadata.ended_at_epoch is not None
    assert metadata.exit_code == 0
    assert metadata.tui_pid == 777
    assert metadata.activity == "finalizing"
    assert metadata.harness_session_id == "ses-native-observed"
    assert metadata.runtime_kind == "path"
    assert metadata.runtime_path == "/usr/local/bin/pi"
    assert metadata.runtime_version == "pi 4.5.6"
    assert metadata.session_dir is not None
    assert metadata.session_dir.endswith("meridian-pi/sessions")
    assert metadata.auth_policy == "inherit-runtime-default-auth-config"
