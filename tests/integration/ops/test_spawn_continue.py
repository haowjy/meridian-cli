"""SpawnContinueInput flow — resume and fork continuation from a source spawn.

SpawnForkInput tests live in test_spawn_fork.py and test_spawn_fork_harness.py.

# qa-validated: test-suite-redesign
"""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnContinueInput,
    SpawnCreateInput,
)
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root


@dataclass(frozen=True)
class _FakeBundleResult:
    model: str
    model_token: str
    harness: HarnessId
    harness_model: str | None
    execution_policy: ResolvedExecutionPolicy
    provenance: dict[str, str]
    warnings: tuple[str, ...] = ()


def _state_root(project_root: Path) -> Path:
    mars_toml = project_root / "mars.toml"
    if not mars_toml.exists():
        mars_toml.write_text(
            '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
            encoding="utf-8",
        )
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _seed_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    harness_session_id: str | None,
    prompt: str = "seed prompt",
    goal: str | None = None,
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
        goal=goal,
        work_id="w-spawn",
        harness_session_id=harness_session_id,
        execution_cwd=execution_cwd,
    )


def _patch_catalog_models(monkeypatch: pytest.MonkeyPatch) -> None:
    codex_entry = AliasEntry(
        alias="gpt-5.3-codex",
        model_id=ModelId("gpt-5.3-codex"),
        resolved_harness=HarnessId.CODEX,
    )
    claude_entry = AliasEntry(
        alias="claude-sonnet-4.5",
        model_id=ModelId("claude-sonnet-4.5"),
        resolved_harness=HarnessId.CLAUDE,
    )

    def resolve_model(self: CatalogSession, name: str) -> AliasEntry:
        return {
            codex_entry.alias: codex_entry,
            claude_entry.alias: claude_entry,
        }[name]

    monkeypatch.setattr(CatalogSession, "resolve_model", resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [codex_entry, claude_entry])


def _stub_bundle_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str = "gpt-5.3-codex",
    harness: HarnessId = HarnessId.CODEX,
) -> None:
    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda request, *, harness_registry: _FakeBundleResult(
            model=model,
            model_token=request.model_override or model,
            harness=harness,
            harness_model=model,
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "provider"},
        ),
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
    _stub_bundle_adapter(monkeypatch)

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
    _stub_bundle_adapter(monkeypatch)

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


def test_spawn_continue_forwards_shared_create_fields_to_spawn_create(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "README.md").write_text("seed", encoding="utf-8")
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p23a", harness_session_id="session-23a")

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
            spawn_id="p23a",
            prompt="follow-up prompt",
            files=("README.md",),
            template_vars=("ticket=123",),
            desc="continue desc",
            work="w-continue",
            verbose=True,
            quiet=True,
            stream=True,
            autocompact=200000,
            effort="high",
            sandbox="workspace-write",
            approval="auto",
            debug=True,
            project_root=project_root.as_posix(),
        )
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.files == ("README.md",)
    assert captured_input.template_vars == ("ticket=123",)
    assert captured_input.desc == "continue desc"
    assert captured_input.work == "w-continue"
    assert captured_input.verbose is True
    assert captured_input.quiet is True
    assert captured_input.stream is True
    assert captured_input.autocompact == 200000
    assert captured_input.effort == "high"
    assert captured_input.sandbox == "workspace-write"
    assert captured_input.approval == "auto"
    assert captured_input.debug is True


def test_spawn_continue_inherits_source_goal_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p23b",
        harness_session_id="session-23b",
        goal="ship inherited goal",
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
            spawn_id="p23b",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.goal == "ship inherited goal"


def test_spawn_continue_goal_override_wins_over_source_goal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p23c",
        harness_session_id="session-23c",
        goal="old goal",
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
            spawn_id="p23c",
            prompt="follow-up prompt",
            goal="  new goal  ",
            project_root=project_root.as_posix(),
        )
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.goal == "new goal"


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


def test_spawn_continue_rejects_cross_harness_from_resolved_model_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p26", harness_session_id="session-26")
    _patch_catalog_models(monkeypatch)

    with pytest.raises(ValueError) as exc_info:
        spawn_api.spawn_continue_sync(
            SpawnContinueInput(
                spawn_id="p26",
                prompt="follow-up prompt",
                model="claude-sonnet-4.5",
                project_root=project_root.as_posix(),
            )
        )

    assert (
        str(exc_info.value)
        == "Cannot continue across harnesses: source is 'codex', target is 'claude'."
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
            goal=payload.goal,
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
