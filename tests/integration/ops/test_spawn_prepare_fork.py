from pathlib import Path

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.launch.request import SessionRequest
from meridian.lib.ops.runtime import build_runtime_from_root_and_config
from meridian.lib.ops.spawn import context_ref
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.spawn.prepare import SpawnCreateArtifacts, build_create_payload
from meridian.lib.state import work_store
from meridian.lib.state.paths import resolve_kb_dir, resolve_project_paths
from tests.support.launch import FakeBundleResult


def _write_minimal_subagent(project_root: Path) -> None:
    agents_dir = project_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "meridian-subagent.md").write_text(
        "---\n"
        "name: meridian-subagent\n"
        "description: Test subagent profile\n"
        "model: gpt-5.3-codex\n"
        "---\n"
        "\n"
        "Test profile body.\n",
        encoding="utf-8",
    )


def _prepare_codex_runtime(project_root: Path):
    _write_minimal_subagent(project_root)
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    harness_registry = get_default_harness_registry()
    codex_adapter = harness_registry.get_subprocess_harness(HarnessId.CODEX)
    return codex_adapter, build_runtime_from_root_and_config(
        project_root, load_config(project_root)
    )


def _stub_context_from_work(
    monkeypatch: pytest.MonkeyPatch,
    work_id: str | None,
    *,
    spawn_id: str = "p123",
) -> None:
    def fake_resolve_context_ref(project_root: Path, ref: str) -> context_ref.ContextRef:
        _ = (project_root, ref)
        return context_ref.SpawnContextRef(
            spawn_id=spawn_id,
            status="succeeded",
            agent="coder",
            desc="prior task",
            model="gpt-5.5",
            harness="codex",
            work_id=work_id,
        )

    monkeypatch.setattr(context_ref, "resolve_context_ref", fake_resolve_context_ref)


def _prompt_surface_text(artifacts: SpawnCreateArtifacts) -> str:
    prepared = artifacts.prepared
    request = artifacts.request
    projected = prepared.projected_content
    return "\n".join(
        part
        for part in (
            request.prompt,
            request.prompt_payload.appended_system_prompt,
            request.prompt_payload.user_turn_content,
            projected.system_prompt if projected is not None else None,
            projected.user_turn_content if projected is not None else None,
        )
        if part
    )


def _stub_bundle_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    model_routes = {
        "claude-sonnet-4.5": ("claude-sonnet-4.5", HarnessId.CLAUDE),
        "gpt-5.5": ("gpt-5.5", HarnessId.CODEX),
        "gpt-5.4-mini": ("gpt-5.4-mini", HarnessId.CODEX),
    }

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> FakeBundleResult:
        _ = harness_registry
        selected_model, selected_harness = model_routes.get(
            request.model_override or "",
            ("gpt-5.3-codex", HarnessId.CODEX),
        )
        return FakeBundleResult(
            model=selected_model,
            model_token=request.model_override or selected_model,
            harness=selected_harness,
            harness_model=selected_model,
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "provider"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)


def test_build_create_payload_inherits_work_from_context_from(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_bundle_adapter(monkeypatch)
    _stub_context_from_work(monkeypatch, "feature-x")
    _, runtime = _prepare_codex_runtime(tmp_path)

    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="continue from prior spawn",
            project_root=tmp_path.as_posix(),
            context_from=("p123",),
        ),
        runtime=runtime,
        ctx=RuntimeContext(),
    )

    assert artifacts.request.task_cwd_work_item == "feature-x"
    assert artifacts.request.inherited_context_work_id == "feature-x"
    assert artifacts.request.task_cwd_source == "ambient-work-authority-root"


def test_build_create_payload_work_precedence_over_context_from(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_bundle_adapter(monkeypatch)
    _stub_context_from_work(monkeypatch, "from-work")
    _, runtime = _prepare_codex_runtime(tmp_path)

    explicit = build_create_payload(
        SpawnCreateInput(
            prompt="continue from prior spawn",
            project_root=tmp_path.as_posix(),
            context_from=("p123",),
            work="explicit-work",
        ),
        runtime=runtime,
        ctx=RuntimeContext(),
    )
    ambient = build_create_payload(
        SpawnCreateInput(
            prompt="continue from prior spawn",
            project_root=tmp_path.as_posix(),
            context_from=("p123",),
        ),
        runtime=runtime,
        ctx=RuntimeContext(work_id="ambient-work"),
    )

    assert explicit.request.task_cwd_work_item == "explicit-work"
    assert explicit.request.inherited_context_work_id is None
    assert explicit.request.task_cwd_source == "explicit-work-authority-root"
    assert ambient.request.task_cwd_work_item == "ambient-work"
    assert ambient.request.inherited_context_work_id is None
    assert ambient.request.task_cwd_source == "ambient-work-authority-root"


def test_background_spawn_prompt_includes_launch_preamble(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_bundle_adapter(monkeypatch)
    _, runtime = _prepare_codex_runtime(tmp_path)

    background = build_create_payload(
        SpawnCreateInput(
            prompt="implement the task",
            project_root=tmp_path.as_posix(),
            background=True,
        ),
        runtime=runtime,
    )
    foreground = build_create_payload(
        SpawnCreateInput(
            prompt="implement the task",
            project_root=tmp_path.as_posix(),
            background=False,
        ),
        runtime=runtime,
    )

    # Both background and foreground spawns are sub-agent sessions
    sub_agent_marker = "This is a **sub-agent session**. You are not talking to the user."
    assert sub_agent_marker in _prompt_surface_text(background)
    assert sub_agent_marker in _prompt_surface_text(foreground)
    # Only background gets the autonomous work directive
    autonomous = "Work autonomously toward your objective. Only escalate if blocked."
    assert autonomous in _prompt_surface_text(background)
    assert autonomous not in _prompt_surface_text(foreground)


def test_build_create_payload_does_not_forward_meridian_primary_or_legacy_defaults_to_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude", ".codex", ".opencode"]\n'
        'default_model = "mars-default-model"\n'
        'default_harness = "opencode"\n',
        encoding="utf-8",
    )
    (tmp_path / "meridian.toml").write_text(
        "[defaults]\n"
        'model = "legacy-default-model"\n'
        'harness = "claude"\n'
        "\n"
        "[primary]\n"
        'model = "primary-model"\n'
        'harness = "codex"\n',
        encoding="utf-8",
    )
    captured_requests: list[bundle_adapter.BundleRequest] = []

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> FakeBundleResult:
        _ = harness_registry
        captured_requests.append(request)
        return FakeBundleResult(
            model="mars-default-model",
            model_token="mars-default-model",
            harness=HarnessId.OPENCODE,
            harness_model="openai/mars-default-model",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "project", "harness_source": "project"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    runtime = build_runtime_from_root_and_config(tmp_path, load_config(tmp_path))

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="use mars project routing defaults",
            project_root=tmp_path.as_posix(),
            dry_run=True,
        ),
        runtime=runtime,
    ).request

    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.model_override is None
    assert request.harness_override is None
    assert prepared.model == "mars-default-model"
    assert prepared.harness == "opencode"


def test_fork_prepare_preserves_continue_fork_and_defers_materialization(
    monkeypatch, tmp_path: Path
) -> None:
    """I-10: build_create_payload must NOT call fork_session.

    Fork materialization is deferred to execute.py (after the spawn row exists).
    prepare.py's job is to preserve continue_fork=True so the executor can act on it.
    """
    codex_adapter, runtime = _prepare_codex_runtime(tmp_path)
    _stub_bundle_adapter(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        codex_adapter,
        "fork_session",
        lambda source_session_id: calls.append(source_session_id) or "forked-session",
    )

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="fork prompt",
            model="gpt-5.4-mini",
            project_root=tmp_path.as_posix(),
            session=SessionRequest(
                requested_harness_session_id="source-session",
                continue_harness="codex",
                continue_fork=True,
            ),
            dry_run=False,
        ),
        runtime=runtime,
    ).request
    dry_run_prepared = build_create_payload(
        SpawnCreateInput(
            prompt="fork prompt",
            model="gpt-5.4-mini",
            project_root=tmp_path.as_posix(),
            session=SessionRequest(
                requested_harness_session_id="source-session",
                continue_harness="codex",
                continue_fork=True,
            ),
            dry_run=True,
        ),
        runtime=runtime,
    ).request

    # I-10: fork_session must NOT be called in prepare — fork happens after the row exists.
    assert calls == []
    # The source session ID and continue_fork=True are preserved for the executor.
    assert prepared.session.requested_harness_session_id == "source-session"
    assert prepared.session.continue_fork is True
    # dry_run also preserves the deferred state.
    assert dry_run_prepared.session.requested_harness_session_id == "source-session"
    assert dry_run_prepared.session.continue_fork is True

    dry_run_command = " ".join(dry_run_prepared.cli_command)
    assert "/spawns/preview/report.md" not in dry_run_command
    assert "<spawn-report-path>" in dry_run_command


def test_build_create_payload_kb_reference_uses_authority_kb_dir_with_external_task_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    _stub_bundle_adapter(monkeypatch)
    runtime = build_runtime_from_root_and_config(project_root, load_config(project_root))

    project_state_dir = resolve_project_paths(project_root).root_dir
    work = work_store.create_work_item(project_state_dir, "feature-x", "", None)
    external_task_cwd = tmp_path / "external-worktree"
    external_task_cwd.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_task_dir(
        project_state_dir,
        work.name,
        task_dir=external_task_cwd.as_posix(),
    )

    authority_kb_file = resolve_kb_dir(project_root) / "domain" / "decision.md"
    authority_kb_file.parent.mkdir(parents=True, exist_ok=True)
    authority_kb_file.write_text("authority kb", encoding="utf-8")
    shadow_kb_file = external_task_cwd / ".meridian" / "kb" / "domain" / "decision.md"
    shadow_kb_file.parent.mkdir(parents=True, exist_ok=True)
    shadow_kb_file.write_text("task shadow kb", encoding="utf-8")

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="check kb",
            model="gpt-5.4-mini",
            project_root=project_root.as_posix(),
            work=work.name,
            files=("kb:domain/decision.md",),
            dry_run=True,
        ),
        runtime=runtime,
    ).request

    assert prepared.task_cwd == external_task_cwd.as_posix()
    assert prepared.reference_anchor == external_task_cwd.as_posix()
    assert tuple(Path(path) for path in prepared.reference_files) == (authority_kb_file.resolve(),)


def test_build_create_payload_relative_reference_resolves_from_selected_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    _stub_bundle_adapter(monkeypatch)
    runtime = build_runtime_from_root_and_config(project_root, load_config(project_root))

    project_state_dir = resolve_project_paths(project_root).root_dir
    work = work_store.create_work_item(project_state_dir, "feature-x", "", None)
    external_task_cwd = tmp_path / "feature-worktree"
    external_task_cwd.mkdir(parents=True, exist_ok=True)
    work_store.update_work_item_task_dir(
        project_state_dir,
        work.name,
        task_dir=external_task_cwd.as_posix(),
    )
    reference_file = external_task_cwd / "notes.md"
    reference_file.write_text("worktree notes", encoding="utf-8")

    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="review notes",
            model="gpt-5.4-mini",
            project_root=project_root.as_posix(),
            work=work.name,
            files=("notes.md",),
            dry_run=True,
        ),
        runtime=runtime,
    ).request

    assert prepared.task_cwd == external_task_cwd.as_posix()
    assert prepared.reference_anchor == external_task_cwd.as_posix()
    assert prepared.task_cwd_source == "explicit-work-task-dir"
    assert prepared.task_cwd_work_item == work.name
    assert tuple(Path(path) for path in prepared.reference_files) == (reference_file.resolve(),)
