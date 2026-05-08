from pathlib import Path
from types import SimpleNamespace

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnContinueInput,
    SpawnCreateInput,
    SpawnForkInput,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root


def _state_root(project_root: Path) -> Path:
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _seed_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    harness_session_id: str | None,
    prompt: str = "seed prompt",
    execution_cwd: str | None = None,
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c-seed",
        model="gpt-5.3-codex",
        agent="coder",
        skills=("skill-c",),
        harness="codex",
        prompt=prompt,
        work_id="w-spawn",
        harness_session_id=harness_session_id,
        execution_cwd=execution_cwd,
    )


def test_spawn_continue_errors_when_source_spawn_lacks_harness_session_id(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p11", harness_session_id=None)

    try:
        spawn_api.spawn_continue_sync(
            SpawnContinueInput(
                spawn_id="p11",
                prompt="follow-up prompt",
                project_root=project_root.as_posix(),
            )
        )
    except ValueError as exc:
        assert str(exc) == "Spawn 'p11' has no recorded session — cannot continue/fork."
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected continue from missing harness session to fail.")


def test_spawn_continue_passes_resume_details_in_session_dto_fields(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p21",
        harness_session_id="session-21",
        execution_cwd="/tmp/source-cwd",
    )

    captured_input: SpawnCreateInput | None = None

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p21",
            prompt="follow-up prompt",
            fork=True,
            project_root=project_root.as_posix(),
        )
    )

    assert result.status == "dry-run"
    assert result.command == "spawn.continue"
    assert captured_input is not None
    assert captured_input.background is False

    # Session DTO carries the canonical continuation payload.
    assert captured_input.session.requested_harness_session_id == "session-21"
    assert captured_input.session.continue_harness == "codex"
    assert captured_input.session.continue_source_tracked is True
    assert captured_input.session.continue_source_ref == "p21"
    assert captured_input.session.continue_fork is True
    assert captured_input.session.continue_chat_id == "c-seed"
    assert captured_input.session.forked_from_chat_id == "c-seed"
    assert captured_input.session.source_execution_cwd == "/tmp/source-cwd"


def test_spawn_continue_respects_explicit_background_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p22", harness_session_id="session-22")

    captured_input: SpawnCreateInput | None = None

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p22",
            prompt="follow-up prompt",
            background=True,
            project_root=project_root.as_posix(),
        )
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.background is True


def test_spawn_continue_passes_explicit_harness_to_create_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p23", harness_session_id="session-23")

    captured_input: SpawnCreateInput | None = None

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p23",
            prompt="follow-up prompt",
            harness="codex",
            project_root=project_root.as_posix(),
        )
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.harness == "codex"


def test_spawn_continue_errors_on_explicit_harness_conflict(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p24", harness_session_id="session-24")

    with pytest.raises(ValueError) as exc_info:
        spawn_api.spawn_continue_sync(
            SpawnContinueInput(
                spawn_id="p24",
                prompt="follow-up prompt",
                harness="claude",
                project_root=project_root.as_posix(),
            )
        )

    assert (
        str(exc_info.value)
        == "Cannot continue spawn 'p24' with harness 'claude'; source spawn uses 'codex'."
    )


def test_spawn_continue_dry_run_with_prepared_context_does_not_require_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)

    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared = prepare_for_runtime_write(project_root)
    assert prepared.runtime_root is not None
    _seed_spawn(prepared.runtime_root, spawn_id="p25", harness_session_id="session-25")

    def _fail_create_lifecycle_service(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry-run prepared continue should not construct lifecycle service")

    monkeypatch.setattr(
        "meridian.lib.core.lifecycle.create_lifecycle_service",
        _fail_create_lifecycle_service,
    )
    monkeypatch.setattr(
        spawn_api,
        "build_create_payload",
        lambda payload, runtime=None, preflight_warning=None, ctx=None: SimpleNamespace(
            harness=payload.harness or "codex",
            model=payload.model,
            warning=preflight_warning,
            agent=payload.agent,
            agent_metadata={},
            skills=payload.skills,
            skill_paths=(),
            reference_files=(),
            template_vars={},
            context_from=(),
            prompt=payload.prompt,
            model_selection_requested_token=None,
            model_selection_canonical_id=None,
            model_selection_harness_provenance=None,
            terminal_surface_mode=None,
            cli_command=("codex",),
        ),
    )

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p25",
            prompt="follow-up prompt",
            dry_run=True,
            project_root=project_root.as_posix(),
        ),
        prepared=prepared,
    )

    assert result.status == "dry-run"
    assert result.command == "spawn.continue"


def test_spawn_fork_inherits_policy_fields_from_resolved_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)

    captured_input: SpawnCreateInput | None = None

    def _fake_resolve_session_reference(*_args, **_kwargs):
        return SimpleNamespace(
            missing_harness_session_id=False,
            harness_session_id="session-seed",
            harness="codex",
            source_model="gpt-5.4",
            source_agent="reviewer",
            source_skills=("skill-a", "skill-b"),
            source_work_id="w-source",
            source_chat_id="c-source",
            source_execution_cwd="/tmp/source-cwd",
            source_claude_config_dir="/tmp/source-claude",
            tracked=True,
        )

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "resolve_session_reference", _fake_resolve_session_reference)
    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_fork_sync(
        SpawnForkInput(
            source_ref="c-source",
            prompt="fork prompt",
            project_root=project_root.as_posix(),
            inherit_source_skills=True,
        )
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.model == "gpt-5.4"
    assert captured_input.agent == "reviewer"
    assert captured_input.skills == ("skill-a", "skill-b")
    assert captured_input.work == "w-source"
    assert captured_input.harness == "codex"
    assert captured_input.session.requested_harness_session_id == "session-seed"
    assert captured_input.session.continue_source_ref == "c-source"
    assert captured_input.session.continue_fork is True
    assert captured_input.session.forked_from_chat_id == "c-source"
    assert captured_input.session.source_execution_cwd == "/tmp/source-cwd"
    assert captured_input.session.source_claude_config_dir == "/tmp/source-claude"


def test_spawn_fork_uses_requested_model_agent_and_skips_source_harness_when_model_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)

    captured_input: SpawnCreateInput | None = None

    monkeypatch.setattr(
        spawn_api,
        "resolve_session_reference",
        lambda *_args, **_kwargs: SimpleNamespace(
            missing_harness_session_id=False,
            harness_session_id="session-seed",
            harness="codex",
            source_model="gpt-5.4",
            source_agent="reviewer",
            source_skills=("skill-a",),
            source_work_id="w-source",
            source_chat_id="c-source",
            source_execution_cwd="/tmp/source-cwd",
            source_claude_config_dir=None,
            tracked=True,
        ),
    )

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_fork_sync(
        SpawnForkInput(
            source_ref="c-source",
            prompt="fork prompt",
            project_root=project_root.as_posix(),
            model="gptmini",
            agent="architect",
            skills=("custom-skill",),
            inherit_source_skills=True,
        )
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.model == "gptmini"
    assert captured_input.agent == "architect"
    assert captured_input.skills == ("custom-skill",)
    assert captured_input.harness is None


def test_spawn_fork_errors_when_reference_has_no_recorded_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)

    monkeypatch.setattr(
        spawn_api,
        "resolve_session_reference",
        lambda *_args, **_kwargs: SimpleNamespace(missing_harness_session_id=True),
    )

    with pytest.raises(ValueError) as exc_info:
        spawn_api.spawn_fork_sync(
            SpawnForkInput(
                source_ref="c7",
                prompt="fork prompt",
                project_root=project_root.as_posix(),
            )
        )

    assert (
        str(exc_info.value)
        == "Session 'c7' has no recorded harness session — cannot continue/fork."
    )
