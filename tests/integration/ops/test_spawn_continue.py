from pathlib import Path
from types import SimpleNamespace

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.ops.spawn.models import SpawnActionOutput, SpawnContinueInput, SpawnCreateInput
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
