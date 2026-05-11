# qa-validated: test-suite-redesign
"""Codex fork and session seeding tests for primary process launch.

Verifies that Codex fork materializes a new session via the adapter,
that native-continue-fork contract skips the fork call, and that the
adapter-derived seed port is used for the seeded session ID.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import ForkMaterializationMode
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import command as launch_command
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.process.primary_attach import PrimaryAttachOutcome
from meridian.lib.launch.process.runner import run_harness_process
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.types import SessionMode
from meridian.lib.state.spawn_store import list_spawns


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


def _build_primary_launch_context(
    *,
    project_root: Path,
    harness_id: HarnessId,
    model: str,
    prompt: str = "primary prompt",
    extra_args: tuple[str, ...] = (),
    session: SessionRequest | None = None,
) -> tuple[Any, Any]:
    _write_minimal_mars_config(project_root)
    harness_registry = get_default_harness_registry()
    config = load_config(project_root)
    launch_context = build_launch_context(
        spawn_id=f"dry-run-primary-{harness_id.value}",
        request=SpawnRequest(
            prompt=prompt,
            prompt_is_composed=False,
            model=model,
            harness=harness_id.value,
            extra_args=extra_args,
            session=session or SessionRequest(),
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
def test_run_harness_process_uses_adapter_primary_seed_port_not_harness_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "codex-seed-port"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CODEX,
        model="gpt-5.4",
    )
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    seeded_session_id = "seeded-via-adapter-port"

    def fake_run_primary_attach(
        harness_id: Any,
        spawn_id: Any,
        spawn_dir: Any,
        execution_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> PrimaryAttachOutcome:
        return PrimaryAttachOutcome(exit_code=0, session_id=None, tui_pid=5150)

    monkeypatch.setattr(
        codex_adapter,
        "derive_primary_seeded_session_id",
        lambda **_kwargs: seeded_session_id,
    )
    monkeypatch.setattr(
        codex_adapter,
        "observe_session_id",
        lambda **kwargs: kwargs.get("current_session_id"),
    )

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fake_run_primary_attach,
        run_primary_process_with_capture_fn=lambda *_args: (_ for _ in ()).throw(
            AssertionError("managed primary path should avoid black-box launcher")
        ),
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert outcome.exit_code == 0
    assert outcome.resolved_harness_session_id == seeded_session_id
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns) == 1
    assert spawns[0].harness_session_id == seeded_session_id


@pytest.mark.slow
def test_run_harness_process_fork_uses_new_chat_and_materialized_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path
    _write_minimal_mars_config(project_root)
    harness_registry = get_default_harness_registry()
    config = load_config(project_root)
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    launch_context = build_launch_context(
        spawn_id="dry-run-primary",
        request=SpawnRequest(
            prompt="fork prompt",
            prompt_is_composed=False,
            model="gpt-5.4",
            harness=HarnessId.CODEX.value,
            session=SessionRequest(
                requested_harness_session_id="source-session",
                continue_chat_id="c7",
                forked_from_chat_id="c7",
                continue_fork=True,
                primary_session_mode=SessionMode.FORK.value,
            ),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            config_snapshot=config.model_dump(mode="json", exclude_none=True),
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=project_root.as_posix(),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )

    captured: dict[str, str | None] = {}

    def fake_project_subprocess_spec(
        harness_id: HarnessId,
        spec: ResolvedLaunchSpec,
        *,
        base_command: tuple[str, ...],
    ) -> list[str]:
        assert harness_id is HarnessId.CODEX
        captured["build_continue_session"] = spec.continue_session_id
        return [*base_command, "resume", spec.continue_session_id or ""]

    def fake_fork_session(source_session_id: str) -> str:
        captured["fork_source_session"] = source_session_id
        return "forked-session"

    def fake_run_primary_attach(
        harness_id: Any,
        spawn_id: Any,
        spawn_dir: Any,
        execution_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> PrimaryAttachOutcome:
        captured["env_chat_id"] = dict(env).get("MERIDIAN_CHAT_ID")
        return PrimaryAttachOutcome(
            exit_code=0,
            session_id="forked-session",
            tui_pid=111,
        )

    def fake_start_session(
        runtime_root: Path,
        harness: str,
        harness_session_id: str | None,
        model: str,
        chat_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        _ = (runtime_root, harness, model)
        captured["chat_id_arg"] = chat_id
        captured["start_harness_session_id"] = harness_session_id
        captured["forked_from_chat_id"] = kwargs.get("forked_from_chat_id")
        return "c999"

    monkeypatch.setattr(
        launch_command,
        "project_subprocess_spec",
        fake_project_subprocess_spec,
    )
    monkeypatch.setattr(codex_adapter, "fork_session", fake_fork_session)
    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: "forked-session")

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fake_run_primary_attach,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
        start_session_fn=fake_start_session,
    )

    assert captured["fork_source_session"] == "source-session"
    assert captured["build_continue_session"] == "forked-session"
    assert captured["chat_id_arg"] is None
    # I-10: session is created with the SOURCE session ID; fork happens after the row exists.
    assert captured["start_harness_session_id"] == "source-session"
    assert captured["forked_from_chat_id"] == "c7"
    assert captured["env_chat_id"] == "c999"
    assert outcome.chat_id == "c999"
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns) == 1
    assert spawns[0].terminal_origin == "launcher"


@pytest.mark.slow
def test_run_harness_process_fork_materialization_comes_from_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path
    _write_minimal_mars_config(project_root)
    harness_registry = get_default_harness_registry()
    config = load_config(project_root)
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    native_fork_contract = codex_adapter.contract.model_copy(
        update={
            "bootstrap": codex_adapter.contract.bootstrap.model_copy(
                update={"fork_materialization": ForkMaterializationMode.NATIVE_CONTINUE_FORK}
            )
        }
    )
    monkeypatch.setattr(
        type(codex_adapter),
        "contract",
        property(lambda _self: native_fork_contract),
    )
    launch_context = build_launch_context(
        spawn_id="dry-run-primary",
        request=SpawnRequest(
            prompt="fork prompt",
            prompt_is_composed=False,
            model="gpt-5.4",
            harness=HarnessId.CODEX.value,
            session=SessionRequest(
                requested_harness_session_id="source-session",
                continue_chat_id="c7",
                forked_from_chat_id="c7",
                continue_fork=True,
                primary_session_mode=SessionMode.FORK.value,
            ),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            config_snapshot=config.model_dump(mode="json", exclude_none=True),
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=project_root.as_posix(),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )

    captured: dict[str, str | None] = {}

    def fake_project_subprocess_spec(
        harness_id: HarnessId,
        spec: ResolvedLaunchSpec,
        *,
        base_command: tuple[str, ...],
    ) -> list[str]:
        assert harness_id is HarnessId.CODEX
        captured["build_continue_session"] = spec.continue_session_id
        return [*base_command, "resume", spec.continue_session_id or ""]

    def fail_if_forked(source_session_id: str) -> str:
        raise AssertionError(f"Unexpected fork materialization for session {source_session_id}")

    def fake_run_primary_attach(
        harness_id: Any,
        spawn_id: Any,
        spawn_dir: Any,
        execution_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> PrimaryAttachOutcome:
        captured["env_chat_id"] = dict(env).get("MERIDIAN_CHAT_ID")
        return PrimaryAttachOutcome(
            exit_code=0,
            session_id="source-session",
            tui_pid=111,
        )

    monkeypatch.setattr(
        launch_command,
        "project_subprocess_spec",
        fake_project_subprocess_spec,
    )
    monkeypatch.setattr(codex_adapter, "fork_session", fail_if_forked)
    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: "source-session")

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fake_run_primary_attach,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
        start_session_fn=lambda *args, **kwargs: "c999",
    )

    assert captured["build_continue_session"] == "source-session"
    assert captured["env_chat_id"] == "c999"
    assert outcome.chat_id == "c999"
