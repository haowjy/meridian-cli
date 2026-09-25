# qa-validated: test-suite-redesign
"""Codex fork and session seeding tests for primary process launch.

Verifies that Codex fork materializes a new session via the adapter,
and that the native-continue-fork contract skips the fork call.
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
from meridian.lib.state import session_store
from meridian.lib.state.spawn_store import list_spawns
from tests.support.launch import stub_bundle_request_and_resolve


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _stub_launch_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
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
                requested_harness_session_id="00000000-0000-4000-8000-000000000001",
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
        if captured.get("fork_source_session"):
            assert spec.native_identity is not None
            assert spec.native_identity.operation == "fork"
            assert (
                spec.native_identity.session_id
                == captured["build_continue_session"]
            )
        return [*base_command, "resume", spec.continue_session_id or ""]

    def fake_fork_session(source_session_id: str) -> str:
        captured["fork_source_session"] = source_session_id
        return "00000000-0000-4000-8000-000000000002"

    def fake_run_primary_attach(
        harness_id: Any,
        spawn_id: Any,
        spawn_dir: Any,
        control_root: Any,
        task_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> PrimaryAttachOutcome:
        _ = (
            harness_id,
            spawn_id,
            spawn_dir,
            control_root,
            task_cwd,
            spec,
            process_launcher,
            on_running,
        )
        captured["env_chat_id"] = dict(env).get("MERIDIAN_CHAT_ID")
        return PrimaryAttachOutcome(
            exit_code=0,
            session_id="00000000-0000-4000-8000-000000000002",
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
        _ = model
        captured["chat_id_arg"] = chat_id
        captured["start_harness_session_id"] = harness_session_id
        captured["forked_from_chat_id"] = kwargs.get("forked_from_chat_id")
        session_store.start_session(
            runtime_root,
            harness=harness,
            harness_session_id=harness_session_id or "",
            model=model,
            chat_id="c999",
            kind="primary",
        )
        return "c999"

    monkeypatch.setattr(
        launch_command,
        "project_subprocess_spec",
        fake_project_subprocess_spec,
    )
    monkeypatch.setattr(codex_adapter, "fork_session", fake_fork_session)
    forked_id = "00000000-0000-4000-8000-000000000002"
    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: forked_id)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fake_run_primary_attach,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
        start_session_fn=fake_start_session,
    )

    assert captured["fork_source_session"] == "00000000-0000-4000-8000-000000000001"
    assert captured["build_continue_session"] == "00000000-0000-4000-8000-000000000002"
    assert captured["chat_id_arg"] is None
    # I-10: fork happens after the row exists; the parent is not the child identity.
    assert captured["start_harness_session_id"] == ""
    assert captured["forked_from_chat_id"] == "c7"
    assert captured["env_chat_id"] == "c999"
    assert outcome.chat_id == "c999"
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns.records) == 1
    assert spawns.records[0].terminal.origin == "launcher"


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
                requested_harness_session_id="00000000-0000-4000-8000-000000000001",
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
        control_root: Any,
        task_cwd: Any,
        env: Any,
        spec: Any,
        process_launcher: Any,
        on_running: Any = None,
    ) -> PrimaryAttachOutcome:
        _ = (
            harness_id,
            spawn_id,
            spawn_dir,
            control_root,
            task_cwd,
            spec,
            process_launcher,
            on_running,
        )
        captured["env_chat_id"] = dict(env).get("MERIDIAN_CHAT_ID")
        return PrimaryAttachOutcome(
            exit_code=0,
            session_id="00000000-0000-4000-8000-000000000001",
            tui_pid=111,
        )

    monkeypatch.setattr(
        launch_command,
        "project_subprocess_spec",
        fake_project_subprocess_spec,
    )
    monkeypatch.setattr(codex_adapter, "fork_session", fail_if_forked)
    source_id = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(codex_adapter, "observe_session_id", lambda **kwargs: source_id)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_attach_fn=fake_run_primary_attach,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
        start_session_fn=lambda *args, **kwargs: "c999",
    )

    assert captured["build_continue_session"] == "00000000-0000-4000-8000-000000000001"
    assert captured["env_chat_id"] == "c999"
    assert outcome.chat_id == "c999"


def test_tracked_codex_fork_runs_shell_in_recorded_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import sqlite3
    import subprocess

    from meridian.lib.ops.reference import ResolvedSessionReference
    from meridian.lib.ops.spawn.api import _build_fork_create_input
    from meridian.lib.ops.spawn.models import SpawnForkInput

    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "meridian-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ambient-decoy"))
    sid = "12345678-1234-4234-8234-123456789abc"
    store = tmp_path / "recorded" / "sessions"
    store.mkdir(parents=True)
    source = store / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n")
    with sqlite3.connect(store.parent / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
        db.execute("INSERT INTO threads VALUES (?, ?)", (sid, str(source)))
    reference = ResolvedSessionReference(
        harness_session_id=sid, harness="codex", source_chat_id="c-source",
        source_model="gpt-5.4", source_agent=None, source_skills=(), source_work_id=None,
        tracked=True, source_native_store=str(store),
    )
    fork = _build_fork_create_input(
        payload=SpawnForkInput(source_ref="c-source", prompt="fork"),
        normalized_source_ref="c-source", resolved_reference=reference,
        requested_model="gpt-5.4", requested_agent=None, inherited_skills=(),
        requested_work="", requested_task_dir=None, requested_goal=None, harness="codex",
    )
    context, registry = _build_primary_launch_context(
        project_root=tmp_path, harness_id=HarnessId.CODEX, model="gpt-5.4",
        session=fork.session.model_copy(update={"primary_session_mode": "fork"}),
    )
    shim = tmp_path / "codex-shim"
    shim.write_text('#!/bin/sh\nprintf "%s\\n%s\\n" "$CODEX_HOME" "$1"\n')
    observed: list[str] = []

    def attach(
        harness_id, spawn_id, log_dir, control_root, task_cwd, env, spec, launcher, on_running,
    ):
        assert spec.native_identity is not None
        target_id = spec.native_identity.session_id
        assert target_id and target_id != sid
        result = subprocess.run(
            ["sh", str(shim), target_id], env=env, cwd=control_root,
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.splitlines() == [str(store.parent), target_id]
        target = registry.get(HarnessId.CODEX).resolve_native_session_file(
             session_id=target_id, native_store=store,
        )
        assert target is not None and target != source
        assert json.loads(target.read_text().splitlines()[0])["payload"]["id"] == target_id
        observed.append(target_id)
        return PrimaryAttachOutcome(exit_code=0, session_id=target_id, tui_pid=None)

    outcome = run_harness_process(context, registry, run_primary_attach_fn=attach)
    assert outcome.exit_code == 0
    assert len(observed) == 1
    chat = session_store.get_session_record(context.runtime_root, outcome.chat_id)
    assert chat is not None and chat.harness_session_id == observed[0]
    assert chat.native_store == str(store)
    assert json.loads(source.read_text())["payload"]["id"] == sid
