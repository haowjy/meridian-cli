# qa-validated: test-suite-redesign
"""Claude session seeding, repair, and resume tests.

Verifies that fresh Claude primary launches seed a --session-id, that
command-generated session IDs are recorded correctly, that observation
repairs diverged state, and that resume launches do not inject seed args.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.process import runner as process_runner
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


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
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
def test_run_harness_process_fresh_claude_primary_seeds_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fresh Claude primary launch seeds --session-id for all launches."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "seed-reuse"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    captured: dict[str, object] = {}

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        command = tuple(command)
        captured["command"] = command
        if "--session-id" in command:
            idx = command.index("--session-id")
            captured["command_session_id"] = command[idx + 1]
        assert callable(on_child_started)
        on_child_started(555)
        return (0, 555)

    monkeypatch.setattr(claude_adapter, "observe_session_id", lambda **kwargs: None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    # No pre-seeded session from the launch context; Claude generates one in the command.
    assert launch_context.seed_harness_session_id in (None, "")
    assert "command_session_id" in captured
    seeded_id = captured["command_session_id"]
    assert outcome.resolved_harness_session_id == seeded_id
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns) == 1
    assert spawns[0].harness_session_id == seeded_id


@pytest.mark.slow
def test_run_harness_process_records_generated_claude_command_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "claude-command-session-id"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    generated_session_id = "generated-command-session-id"
    original_build_launch_context = process_runner.build_launch_context

    def fake_build_launch_context(*args: object, **kwargs: object) -> Any:
        runtime_context = original_build_launch_context(*args, **kwargs)
        # Strip any --session-id already injected by resolve_launch_spec so the
        # test-controlled value is the only one present in the command.
        base_argv = runtime_context.binding.argv
        stripped: list[str] = []
        skip_next = False
        for token in base_argv:
            if skip_next:
                skip_next = False
                continue
            if token == "--session-id":
                skip_next = True
                continue
            if token.startswith("--session-id="):
                continue
            stripped.append(token)
        new_argv = (*stripped, "--session-id", generated_session_id)
        updated_binding = replace(
            runtime_context.binding,
            argv=new_argv,
            effective_harness_session_id="",
        )
        return replace(
            runtime_context,
            binding=updated_binding,
            seed_harness_session_id="",
        )

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        assert callable(on_child_started)
        on_child_started(556)
        return (0, 556)

    monkeypatch.setattr(process_runner, "build_launch_context", fake_build_launch_context)
    monkeypatch.setattr(claude_adapter, "observe_session_id", lambda **kwargs: None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
    )

    assert outcome.resolved_harness_session_id == generated_session_id
    assert outcome.chat_id is not None
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns) == 1
    assert spawns[0].harness_session_id == generated_session_id
    assert outcome.chat_id is not None
    assert (
        session_store.get_session_harness_id(launch_context.runtime_root, outcome.chat_id)
        == generated_session_id
    )


@pytest.mark.slow
def test_run_harness_process_repairs_state_when_observed_session_differs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Observation repairs state when harness uses a different session than persisted."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "seed-repair"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    observed_id = "observed-different-session"

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        assert callable(on_child_started)
        on_child_started(666)
        return (0, 666)

    monkeypatch.setattr(
        claude_adapter, "observe_session_id", lambda **kwargs: observed_id
    )

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    # State should be repaired to the observed session ID
    assert outcome.resolved_harness_session_id == observed_id
    # Spawn record should have the observed ID
    spawns = list_spawns(launch_context.runtime_root)
    assert any(spawn.harness_session_id == observed_id for spawn in spawns)


@pytest.mark.slow
def test_run_harness_process_resume_does_not_inject_seed_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resume launches must not inject seed session args into passthrough."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "seed-resume"
    project_root.mkdir()
    launch_context, _harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        session=SessionRequest(
            requested_harness_session_id="existing-session-id",
            continue_chat_id="c42",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    # Resume path: adapter returns the existing session ID, no session_args injection
    assert launch_context.seed_harness_session_args == ()
    assert launch_context.seed_harness_session_id == "existing-session-id"
