# qa-validated: test-suite-redesign
"""Claude session seeding, repair, and resume tests.

Verifies that fresh Claude primary launches seed a --session-id, that
command-generated session IDs remain provisional, that observation
binds the actual conversation, and that resume launches do not inject seed args.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import meridian.lib.launch.context as launch_context_module
from meridian.lib.config.settings import load_config
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.claude import project_slug
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
from meridian.lib.launch.types import SessionMode
from meridian.lib.ops.reference import UntrackedSourceUse
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
        model="claude-sonnet-4-5",
        harness=HarnessId.CLAUDE,
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
            launch_policy_snapshot=(
                LaunchPolicySnapshot(model=model, harness=harness_id.value)
                if session is not None else None
            ),
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


def _no_observed_session(**kwargs: object) -> None:
    _ = kwargs
    return None


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
    source_lookups: list[object] = []
    monkeypatch.setattr(
        "meridian.lib.ops.reference.resolve_source_use",
        lambda *args: source_lookups.append(args),
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

    monkeypatch.setattr(claude_adapter, "observe_session_id", _no_observed_session)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    # No pre-seeded session from the launch context; Claude generates one in the command.
    assert launch_context.seed_harness_session_id in (None, "")
    assert source_lookups == []
    assert "command_session_id" in captured
    seeded_id = captured["command_session_id"]
    # Command seeds remain hints until the harness actually observes the conversation.
    assert outcome.resolved_harness_session_id == ""
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns.records) == 1
    assert spawns.records[0].harness_session_id == seeded_id
    session = session_store.get_session_record(launch_context.runtime_root, outcome.chat_id)
    assert session is not None
    assert session.spawn_id == spawns.records[0].id
    assert not session.harness_session_id


def test_runner_rejects_independent_prepared_tracked_claim_before_session_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A clean preview cannot hide a conflicting independently supplied preparation."""
    project_root = tmp_path / "prepared-conflict"
    project_root.mkdir()
    context, registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    prepared = launch_context_module._build_direct_surface(
        request=context.resolved_request,
        project_root=project_root,
        reference_anchor=project_root,
        runtime_root=context.runtime_root,
        harness_registry=registry,
    )
    prepared = prepared.__class__(
        **{
            **prepared.__dict__,
            "request": prepared.request.model_copy(
                update={
                    "session": prepared.request.session.model_copy(
                        update={"continue_source_tracked": True}
                    )
                }
            ),
        }
    )
    session_starts: list[bool] = []

    with pytest.raises(ValueError, match="source selection conflict"):
        run_harness_process(
            context,
            registry,
            prepared=prepared,
            start_session_fn=lambda **kwargs: session_starts.append(True) or "unexpected",
        )

    assert session_starts == []


def test_runner_revalidates_untracked_source_once_before_composing_missing_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No-prepared runner path checks the source once, then privately binds it."""
    project_root = tmp_path / "runner-source-use"
    project_root.mkdir()
    calls: list[str] = []

    def resolve_source_use(runtime_root, operation, native_id, harness):
        calls.append(native_id)
        return UntrackedSourceUse(
            operation=operation,
            original_ref=native_id,
            native_id=native_id,
            harness=harness,
            lookup_scope=runtime_root,
        )

    monkeypatch.setattr("meridian.lib.ops.reference.resolve_source_use", resolve_source_use)
    context, registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        session=SessionRequest(
            requested_harness_session_id="native-A",
            continue_source_ref="native-A",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    assert calls == ["native-A"]  # independent preview boundary
    calls.clear()
    adapter = registry.get_subprocess_harness(HarnessId.CLAUDE)
    monkeypatch.setattr(adapter, "observe_session_id", _no_observed_session)

    def fake_process(command, cwd, env, output_log_path, on_child_started=None):
        assert any("native-A" in argument for argument in command)
        assert callable(on_child_started)
        on_child_started(123)
        return 0, 123

    run_harness_process(
        context,
        registry,
        run_primary_process_with_capture_fn=fake_process,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert calls == ["native-A"]  # one runner query; private bind does not re-query


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

    def observed_session(**kwargs: object) -> str:
        _ = kwargs
        return observed_id

    monkeypatch.setattr(claude_adapter, "observe_session_id", observed_session)

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
    assert any(spawn.harness_session_id == observed_id for spawn in spawns.records)


@pytest.mark.slow
def test_run_harness_process_reconciles_claude_tui_trampoline_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())
    project_root = tmp_path / "claude-tui-trampoline"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    real_session_id = "9a4846b0-5380-461d-98cb-304e7cee6e64"

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        command = tuple(command)
        recorded_session_id = command[command.index("--session-id") + 1]
        project_dir = fake_home / ".claude" / "projects" / project_slug(project_root)
        project_dir.mkdir(parents=True)
        (project_dir / f"{real_session_id}.jsonl").write_text(
            "\n".join(
                (
                    json.dumps({"type": "agent-setting", "sessionId": real_session_id}),
                    json.dumps(
                        {
                            "type": "user",
                            "message": {"role": "user", "content": "real prompt"},
                            "timestamp": 1781827539538,
                            "sessionId": real_session_id,
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (fake_home / ".claude" / "history.jsonl").write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "display": "/tui fullscreen",
                            "project": project_root.as_posix(),
                            "sessionId": recorded_session_id,
                            "timestamp": 1781827479996,
                        }
                    ),
                    json.dumps(
                        {
                            "display": "real prompt",
                            "project": project_root.as_posix(),
                            "sessionId": real_session_id,
                            "timestamp": 1781827539538,
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        assert callable(on_child_started)
        on_child_started(667)
        return (0, 667)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
    )

    assert outcome.resolved_harness_session_id == real_session_id
    assert outcome.chat_id is not None
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns.records) == 1
    assert spawns.records[0].harness_session_id == real_session_id
    assert (
        session_store.get_session_harness_id(launch_context.runtime_root, outcome.chat_id)
        == real_session_id
    )


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
