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
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.history import iter_history_events
from meridian.lib.state.primary_meta import read_primary_metadata


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


def _build_pi_primary_launch_context(
    project_root: Path,
    *,
    requested_task_cwd: Path | None = None,
    requested_harness_session_id: str | None = None,
    source_pi_session_dir: str | None = None,
    extra_args: tuple[str, ...] = (),
) -> tuple[Any, Any]:
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
            session=SessionRequest(
                requested_harness_session_id=requested_harness_session_id,
                source_pi_session_dir=source_pi_session_dir,
            ),
            extra_args=extra_args,
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            config_snapshot=config.model_dump(mode="json", exclude_none=True),
            runtime_root=(project_root / ".meridian").as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=project_root.as_posix(),
            requested_task_cwd=(
                requested_task_cwd.as_posix() if requested_task_cwd is not None else None
            ),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )
    return launch_context, harness_registry


def _write_pi_session_file(
    *,
    session_root: Path,
    filename: str,
    session_id: str,
    cwd: Path,
) -> None:
    session_root.mkdir(parents=True, exist_ok=True)
    (session_root / filename).write_text(
        f'{{"type":"session","id":"{session_id}","cwd":"{cwd}"}}\n',
        encoding="utf-8",
    )


@pytest.mark.slow
def test_run_harness_process_pi_primary_persists_native_metadata_at_runner_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    launch_context, harness_registry = _build_pi_primary_launch_context(
        project_root,
        extra_args=(
            "--api-key",
            "super-secret",
            "--auth-token=secret-token",
            "--profile",
            "safe",
        ),
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
        session_root = Path(env["PI_CODING_AGENT_SESSION_DIR"])
        session_root.mkdir(parents=True, exist_ok=True)
        (session_root / "20260518T010203_abc.jsonl").write_text(
            f'{{"type":"session","id":"ses-native-observed","cwd":"{project_root}"}}\n',
            encoding="utf-8",
        )
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
    assert "--api-key" in command
    assert command[command.index("--api-key") + 1] == "super-secret"
    assert "--auth-token=secret-token" in command
    assert "--mode" not in command
    assert "--no-extensions" not in command
    assert "--no-skills" not in command
    assert "--no-context-files" not in command
    assert "--no-prompt-templates" not in command
    extension_values = [
        command[index + 1]
        for index, token in enumerate(command)
        if token == "-e"
    ]
    assert len(extension_values) == 1
    extension_parts = Path(extension_values[0]).parts
    assert extension_parts[-2:] == ("meridian-lifecycle", "index.js")
    assert not any(
        Path(path).parts[-2:] == ("managed-bash", "index.js")
        for path in extension_values
    )
    assert captured["output_log_path"] is None
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["MERIDIAN_PI_BINARY"] == "/usr/local/bin/pi"
    assert env["MERIDIAN_PI_SESSION_ROLE"] == "primary"
    assert env["MERIDIAN_PI_LIFECYCLE_EVENT_FILE"].endswith("pi-lifecycle-events.jsonl")
    assert env["PI_CODING_AGENT_SESSION_DIR"].endswith("meridian-pi/sessions")
    assert "PI_CODING_AGENT_DIR" not in env
    assert "MERIDIAN_PI_WRAPPER_METADATA_PATH" not in env

    metadata = read_primary_metadata(launch_context.runtime_root, str(outcome.primary_spawn_id))
    assert metadata is not None
    assert metadata.managed_backend is False
    assert metadata.command is not None
    assert metadata.command[0] == "/usr/local/bin/pi"
    assert "--api-key" in metadata.command
    assert metadata.command[metadata.command.index("--api-key") + 1] == "<redacted>"
    assert "--auth-token=<redacted>" in metadata.command
    assert "super-secret" not in metadata.command
    assert "--auth-token=secret-token" not in metadata.command
    assert "--profile" in metadata.command
    assert metadata.command[metadata.command.index("--profile") + 1] == "safe"
    assert metadata.launch_cwd == str(project_root)
    assert metadata.started_at_epoch is not None
    assert metadata.ended_at_epoch is not None
    assert metadata.exit_code == 0
    assert metadata.tui_pid == 777
    assert metadata.activity == "finalizing"
    assert metadata.harness_session_id == "ses-native-observed"
    assert metadata.harness_session_discovery == "ok"
    assert metadata.harness_session_discovery_detail is None
    assert metadata.runtime_kind == "path"
    assert metadata.runtime_path == "/usr/local/bin/pi"
    assert metadata.runtime_version == "pi 4.5.6"
    assert metadata.session_dir is not None
    assert metadata.session_dir.endswith("meridian-pi/sessions")
    assert metadata.auth_policy == "inherit-runtime-default-auth-config"


@pytest.mark.slow
def test_run_harness_process_pi_primary_discovery_promotes_env_scoped_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary-env-scoped-discovery"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    custom_session_dir = tmp_path / "custom-session-root"
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(custom_session_dir))
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    launch_context, harness_registry = _build_pi_primary_launch_context(project_root)
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 4.5.6",
        ),
    )

    def fake_run_primary_process_with_capture(
        _command: tuple[str, ...],
        _cwd: Path,
        env: dict[str, str],
        _output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        session_root = Path(env["PI_CODING_AGENT_SESSION_DIR"])
        session_root.mkdir(parents=True, exist_ok=True)
        (session_root / "20260518T010203_env_scoped.jsonl").write_text(
            f'{{"type":"session","id":"ses-env-scoped","cwd":"{project_root}"}}\n',
            encoding="utf-8",
        )
        assert callable(on_child_started)
        on_child_started(901)
        return (0, 901)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert outcome.primary_spawn_id is not None
    spawn_id = str(outcome.primary_spawn_id)
    metadata = read_primary_metadata(launch_context.runtime_root, spawn_id)
    assert metadata is not None
    assert metadata.harness_session_id == "ses-env-scoped"
    assert metadata.harness_session_discovery == "ok"
    row = spawn_store.get_spawn(launch_context.runtime_root, spawn_id)
    assert row is not None
    assert row.harness_session_id == "ses-env-scoped"


@pytest.mark.slow
def test_run_harness_process_pi_primary_launch_env_discovery_overrides_default_root_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary-discovery-authority"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    custom_session_dir = tmp_path / "custom-session-root"
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(custom_session_dir))
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    launch_context, harness_registry = _build_pi_primary_launch_context(project_root)
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 4.5.6",
        ),
    )

    def fake_run_primary_process_with_capture(
        _command: tuple[str, ...],
        _cwd: Path,
        env: dict[str, str],
        _output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        _write_pi_session_file(
            session_root=user_home / "meridian-pi" / "sessions",
            filename="20260518T010203_wrong_default.jsonl",
            session_id="wrong-default",
            cwd=project_root,
        )
        _write_pi_session_file(
            session_root=Path(env["PI_CODING_AGENT_SESSION_DIR"]),
            filename="20260518T010204_correct_custom.jsonl",
            session_id="correct-custom",
            cwd=project_root,
        )
        assert callable(on_child_started)
        on_child_started(903)
        return (0, 903)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
    )

    assert outcome.primary_spawn_id is not None
    assert outcome.chat_id is not None
    spawn_id = str(outcome.primary_spawn_id)
    metadata = read_primary_metadata(launch_context.runtime_root, spawn_id)
    assert metadata is not None
    assert metadata.harness_session_id == "correct-custom"
    assert metadata.harness_session_discovery == "ok"
    row = spawn_store.get_spawn(launch_context.runtime_root, spawn_id)
    assert row is not None
    assert row.harness_session_id == "correct-custom"
    assert session_store.get_session_harness_id(launch_context.runtime_root, outcome.chat_id) == (
        "correct-custom"
    )
    assert "wrong-default" not in session_store.get_session_harness_ids(
        launch_context.runtime_root,
        outcome.chat_id,
    )


@pytest.mark.slow
def test_run_harness_process_pi_primary_uses_runtime_child_cwd_for_launch_and_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary-cwd"
    project_root.mkdir()
    task_cwd = project_root / "nested-task"
    task_cwd.mkdir()
    user_home = tmp_path / "user-home"
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    launch_context, harness_registry = _build_pi_primary_launch_context(
        project_root,
        requested_task_cwd=task_cwd,
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
        _command: tuple[str, ...],
        cwd: Path,
        env: dict[str, str],
        _output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        captured["cwd"] = cwd
        session_root = Path(env["PI_CODING_AGENT_SESSION_DIR"])
        session_root.mkdir(parents=True, exist_ok=True)
        (session_root / "20260518T010203_cwd.jsonl").write_text(
            f'{{"type":"session","id":"ses-native-child-cwd","cwd":"{task_cwd}"}}\n',
            encoding="utf-8",
        )
        assert callable(on_child_started)
        on_child_started(888)
        return (0, 888)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert captured["cwd"] == task_cwd
    assert outcome.primary_spawn_id is not None
    metadata = read_primary_metadata(launch_context.runtime_root, str(outcome.primary_spawn_id))
    assert metadata is not None
    assert metadata.launch_cwd == str(task_cwd)
    assert metadata.harness_session_id == "ses-native-child-cwd"
    assert metadata.harness_session_discovery == "ok"


@pytest.mark.slow
def test_run_harness_process_pi_primary_malformed_session_file_records_discovery_failed_detail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary-malformed-session-file"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    launch_context, harness_registry = _build_pi_primary_launch_context(project_root)
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 4.5.6",
        ),
    )

    def fake_run_primary_process_with_capture(
        _command: tuple[str, ...],
        _cwd: Path,
        env: dict[str, str],
        _output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        session_root = Path(env["PI_CODING_AGENT_SESSION_DIR"])
        session_root.mkdir(parents=True, exist_ok=True)
        (session_root / "20260518T010203_broken.jsonl").write_text(
            "{bad-json\n",
            encoding="utf-8",
        )
        assert callable(on_child_started)
        on_child_started(894)
        return (0, 894)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert outcome.primary_spawn_id is not None
    spawn_id = str(outcome.primary_spawn_id)
    metadata = read_primary_metadata(launch_context.runtime_root, spawn_id)
    assert metadata is not None
    assert metadata.harness_session_id is None
    assert metadata.harness_session_discovery == "discovery_failed"
    assert metadata.harness_session_discovery_detail is not None
    assert metadata.harness_session_discovery_detail.startswith("session_file_parse_error:")
    assert "20260518T010203_broken.jsonl" in metadata.harness_session_discovery_detail
    row = spawn_store.get_spawn(launch_context.runtime_root, spawn_id)
    assert row is not None
    assert row.harness_session_id == ""
    if outcome.chat_id is not None:
        assert session_store.get_session_harness_id(
            launch_context.runtime_root,
            outcome.chat_id,
        ) in {None, ""}


@pytest.mark.slow
def test_run_harness_process_pi_primary_no_session_marks_ephemeral_even_with_unrelated_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary-no-session"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    launch_context, harness_registry = _build_pi_primary_launch_context(
        project_root,
        extra_args=("--no-session",),
    )
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 4.5.6",
        ),
    )

    def fake_run_primary_process_with_capture(
        _command: tuple[str, ...],
        _cwd: Path,
        env: dict[str, str],
        _output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        session_root = Path(env["PI_CODING_AGENT_SESSION_DIR"])
        session_root.mkdir(parents=True, exist_ok=True)
        unrelated_cwd = project_root / "different"
        unrelated_cwd.mkdir()
        (session_root / "20260518T010203_unrelated.jsonl").write_text(
            f'{{"type":"session","id":"ses-unrelated","cwd":"{unrelated_cwd}"}}\n',
            encoding="utf-8",
        )
        assert callable(on_child_started)
        on_child_started(889)
        return (0, 889)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert outcome.primary_spawn_id is not None
    metadata = read_primary_metadata(launch_context.runtime_root, str(outcome.primary_spawn_id))
    assert metadata is not None
    assert metadata.harness_session_discovery == "never_created"
    assert metadata.harness_session_discovery_detail == "ephemeral_session"


@pytest.mark.slow
def test_run_harness_process_pi_primary_no_session_ignores_wrong_default_root_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary-no-session-wrong-default"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    custom_session_dir = tmp_path / "custom-session-root"
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    launch_context, harness_registry = _build_pi_primary_launch_context(
        project_root,
        source_pi_session_dir=custom_session_dir.as_posix(),
        extra_args=("--no-session",),
    )
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 4.5.6",
        ),
    )

    def fake_run_primary_process_with_capture(
        _command: tuple[str, ...],
        _cwd: Path,
        env: dict[str, str],
        _output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        assert env["PI_CODING_AGENT_SESSION_DIR"] == custom_session_dir.as_posix()
        _write_pi_session_file(
            session_root=user_home / "meridian-pi" / "sessions",
            filename="20260518T010204_wrong_default.jsonl",
            session_id="wrong-default",
            cwd=project_root,
        )
        custom_session_dir.mkdir(parents=True, exist_ok=True)
        assert callable(on_child_started)
        on_child_started(895)
        return (0, 895)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
    )

    assert outcome.primary_spawn_id is not None
    assert outcome.chat_id is not None
    spawn_id = str(outcome.primary_spawn_id)
    metadata = read_primary_metadata(launch_context.runtime_root, spawn_id)
    assert metadata is not None
    assert metadata.harness_session_id is None
    assert metadata.harness_session_discovery == "never_created"
    assert metadata.harness_session_discovery_detail == "ephemeral_session"
    row = spawn_store.get_spawn(launch_context.runtime_root, spawn_id)
    assert row is not None
    assert row.harness_session_id == ""
    assert session_store.get_session_harness_id(launch_context.runtime_root, outcome.chat_id) in {
        None,
        "",
    }
    assert "wrong-default" not in session_store.get_session_harness_ids(
        launch_context.runtime_root,
        outcome.chat_id,
    )


@pytest.mark.slow
def test_run_harness_process_pi_primary_continue_session_keeps_discovery_ok_without_new_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary-continue"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    source_session_dir = tmp_path / "custom-source-session-dir"
    launch_context, harness_registry = _build_pi_primary_launch_context(
        project_root,
        requested_harness_session_id="ses-existing",
        source_pi_session_dir=source_session_dir.as_posix(),
    )
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 4.5.6",
        ),
    )

    def fake_run_primary_process_with_capture(
        _command: tuple[str, ...],
        _cwd: Path,
        env: dict[str, str],
        _output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        assert env["PI_CODING_AGENT_SESSION_DIR"] == source_session_dir.as_posix()
        source_session_dir.mkdir(parents=True, exist_ok=True)
        assert callable(on_child_started)
        on_child_started(890)
        return (0, 890)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert outcome.primary_spawn_id is not None
    metadata = read_primary_metadata(launch_context.runtime_root, str(outcome.primary_spawn_id))
    assert metadata is not None
    assert metadata.harness_session_id == "ses-existing"
    assert metadata.harness_session_discovery == "ok"
    assert metadata.harness_session_discovery_detail is None
    assert metadata.session_dir == source_session_dir.as_posix()


@pytest.mark.slow
def test_run_harness_process_pi_primary_continue_session_nonzero_exit_keeps_discovery_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary-continue-failure"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    source_session_dir = tmp_path / "custom-source-session-dir"
    launch_context, harness_registry = _build_pi_primary_launch_context(
        project_root,
        requested_harness_session_id="ses-existing",
        source_pi_session_dir=source_session_dir.as_posix(),
    )
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 4.5.6",
        ),
    )

    def fake_run_primary_process_with_capture(
        _command: tuple[str, ...],
        _cwd: Path,
        env: dict[str, str],
        _output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        _write_pi_session_file(
            session_root=user_home / "meridian-pi" / "sessions",
            filename="20260518T010205_wrong_default.jsonl",
            session_id="wrong-default",
            cwd=project_root,
        )
        assert env["PI_CODING_AGENT_SESSION_DIR"] == source_session_dir.as_posix()
        source_session_dir.mkdir(parents=True, exist_ok=True)
        assert callable(on_child_started)
        on_child_started(892)
        return (19, 892)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
    )

    assert outcome.primary_spawn_id is not None
    assert outcome.chat_id is not None
    metadata = read_primary_metadata(launch_context.runtime_root, str(outcome.primary_spawn_id))
    assert metadata is not None
    assert metadata.harness_session_id == "ses-existing"
    assert metadata.harness_session_discovery == "never_created"
    assert metadata.harness_session_discovery_detail is not None
    assert "no_session_files_in_dir" in metadata.harness_session_discovery_detail
    assert metadata.exit_code == 19
    row = spawn_store.get_spawn(launch_context.runtime_root, str(outcome.primary_spawn_id))
    assert row is not None
    assert row.harness_session_id == "ses-existing"
    assert session_store.get_session_harness_id(launch_context.runtime_root, outcome.chat_id) == (
        "ses-existing"
    )
    assert "wrong-default" not in session_store.get_session_harness_ids(
        launch_context.runtime_root,
        outcome.chat_id,
    )


@pytest.mark.slow
def test_run_harness_process_pi_primary_sidecar_lifecycle_is_projected_to_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary-sidecar-history"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    launch_context, harness_registry = _build_pi_primary_launch_context(project_root)
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 4.5.6",
        ),
    )

    def fake_run_primary_process_with_capture(
        _command: tuple[str, ...],
        _cwd: Path,
        env: dict[str, str],
        _output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        assert callable(on_child_started)
        on_child_started(891)
        sidecar_path = Path(env["MERIDIAN_PI_LIFECYCLE_EVENT_FILE"])
        spawn_id = sidecar_path.parent.name
        sidecar_path.write_text(
            "\n".join(
                (
                    '{"type":"meridian.subspawn.start","schema_version":1,'
                    f'"parent_spawn_id":"{spawn_id}","correlation_id":"c-1",'
                    '"subspawn_id":"j-1","emitted_at_ms":1760000000000}',
                    '{"type":"meridian.notification.queued","schema_version":1,'
                    f'"parent_spawn_id":"{spawn_id}","correlation_id":"c-2",'
                    '"emitted_at_ms":1760000000001}',
                )
            )
            + "\n",
            encoding="utf-8",
        )
        Path(env["MERIDIAN_PRIMARY_STDERR_LOG_PATH"]).write_text(
            "plain stderr diagnostics\n",
            encoding="utf-8",
        )
        return (0, 891)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert outcome.primary_spawn_id is not None
    spawn_dir = launch_context.runtime_root / "spawns" / str(outcome.primary_spawn_id)
    assert (spawn_dir / "stderr.log").is_file()
    assert (spawn_dir / "pi-lifecycle-events.jsonl").is_file()
    runner_history = list(iter_history_events(spawn_dir / "history.jsonl"))
    assert [event["event_type"] for event in runner_history] == [
        "meridian.subspawn.start",
        "meridian.lifecycle.parse_error",
    ]


@pytest.mark.slow
def test_run_harness_process_pi_primary_sidecar_ignores_truncated_final_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "pi-primary-sidecar-truncated-line"
    project_root.mkdir()
    user_home = tmp_path / "user-home"
    monkeypatch.setattr("meridian.lib.harness.pi.get_user_home", lambda: user_home)
    monkeypatch.setattr("meridian.lib.harness.extractors.pi.get_user_home", lambda: user_home)
    launch_context, harness_registry = _build_pi_primary_launch_context(project_root)
    monkeypatch.setattr(
        "meridian.lib.harness.pi.resolve_pi_runtime",
        lambda **_kwargs: PiRuntimeResolution(
            binary_path="/usr/local/bin/pi",
            runtime_kind="path",
            runtime_version="pi 4.5.6",
        ),
    )

    def fake_run_primary_process_with_capture(
        _command: tuple[str, ...],
        _cwd: Path,
        env: dict[str, str],
        _output_log_path: Path | None,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        assert callable(on_child_started)
        on_child_started(902)
        sidecar_path = Path(env["MERIDIAN_PI_LIFECYCLE_EVENT_FILE"])
        spawn_id = sidecar_path.parent.name
        sidecar_path.write_text(
            "\n".join(
                (
                    '{"type":"meridian.subspawn.start","schema_version":1,'
                    f'"parent_spawn_id":"{spawn_id}","correlation_id":"c-1",'
                    '"subspawn_id":"j-1","emitted_at_ms":1760000000000}',
                )
            )
            + "\n"
            + '{"type":"meridian.subspawn.end","schema_version":1,'
            f'"parent_spawn_id":"{spawn_id}","correlation_id":"c-2"',
            encoding="utf-8",
        )
        return (0, 902)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert outcome.primary_spawn_id is not None
    spawn_dir = launch_context.runtime_root / "spawns" / str(outcome.primary_spawn_id)
    runner_history = list(iter_history_events(spawn_dir / "history.jsonl"))
    assert [event["event_type"] for event in runner_history] == ["meridian.subspawn.start"]
