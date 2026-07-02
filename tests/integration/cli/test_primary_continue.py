# qa-validated: test-suite-redesign
"""Primary --continue launch-policy snapshot replay and legacy fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from meridian.cli.primary_launch import run_primary_launch
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.core.domain import SkillContent
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import LaunchRequest, LaunchResult, compile_prepared_policy_surface
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.plan import build_primary_launch_runtime, build_primary_spawn_request
from meridian.lib.launch.process import ProcessOutcome
from meridian.lib.launch.request import SessionRequest
from meridian.lib.launch.types import SessionMode
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root
from tests.support.fixtures import write_skill
from tests.support.launch import stub_bundle_request_and_resolve


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


def _seed_primary_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    harness_session_id: str,
    work_id: str | None = None,
    task_cwd: str | None = None,
    launch_policy_snapshot: LaunchPolicySnapshot | None = None,
) -> None:
    model = launch_policy_snapshot.model if launch_policy_snapshot is not None else "gpt-5.3-codex"
    agent = (
        (launch_policy_snapshot.agent or "agent-a")
        if launch_policy_snapshot is not None
        else "agent-a"
    )
    skills = launch_policy_snapshot.skills if launch_policy_snapshot is not None else ("skill-a",)
    harness = launch_policy_snapshot.harness if launch_policy_snapshot is not None else "codex"
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c-primary",
        model=model,
        agent=agent,
        skills=skills,
        harness=harness,
        kind="primary",
        prompt="primary prompt",
        work_id=work_id,
        task_cwd=task_cwd,
        harness_session_id=harness_session_id,
        launch_policy_snapshot=launch_policy_snapshot,
    )


def test_primary_continue_replays_source_launch_policy_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    source_task_dir = tmp_path / "source-worktree"
    source_task_dir.mkdir()
    runtime_root = _state_root(project_root)
    write_skill(project_root, "testing-principles")
    snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness="claude",
        agent="agent-a",
        skills=("testing-principles",),
        loaded_skills=(
            SkillContent(
                name="testing-principles",
                description="testing-principles skill",
                path="/skills/testing-principles/SKILL.md",
                content="# testing-principles\n\nBe consistent.\n",
                skill_type="reference",
            ),
        ),
        execution_policy=ResolvedExecutionPolicy(approval="auto", sandbox="workspace-write"),
        tools={"write": "allow"},
        mcp_tools=("github",),
        extra_args=("--permission-mode", "acceptEdits"),
    )
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p41",
        harness_session_id="session-41",
        work_id="source-work",
        task_cwd=source_task_dir.as_posix(),
        launch_policy_snapshot=snapshot,
    )
    monkeypatch.setenv("MERIDIAN_MODEL", "gpt-5.4")
    monkeypatch.setenv("MERIDIAN_AGENT", "agent-b")
    monkeypatch.setenv("MERIDIAN_APPROVAL", "confirm")
    monkeypatch.setenv("MERIDIAN_SANDBOX", "read-only")

    captured: dict[str, LaunchRequest] = {}

    def fake_launch_primary(
        *,
        project_root: Path,
        request: LaunchRequest,
        harness_registry: object,
    ) -> LaunchResult:
        _ = (project_root, harness_registry)
        captured["request"] = request
        return LaunchResult(
            command=(),
            exit_code=0,
            continue_ref="session-41",
            continue_chat_id="c41",
        )

    with patch("meridian.cli.primary_launch.launch_primary", side_effect=fake_launch_primary):
        run_primary_launch(
            project_root=project_root,
            continue_ref="p41",
            fork_ref=None,
            fork_fresh_ref=None,
            model="",
            harness=None,
            agent=None,
            work="",
            task_dir=None,
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=False,
            passthrough=(),
            skills=(),
        )

    request = captured["request"]
    assert request.work_id == "source-work"
    assert request.task_dir == source_task_dir.as_posix()
    assert request.session.source_execution_cwd == source_task_dir.as_posix()
    assert request.launch_policy_snapshot == snapshot
    assert request.launch_policy_snapshot is not None
    assert request.launch_policy_snapshot.model == snapshot.model
    assert request.launch_policy_snapshot.harness == snapshot.harness
    assert request.launch_policy_snapshot.agent == snapshot.agent
    assert request.launch_policy_snapshot.skills == snapshot.skills
    assert request.launch_policy_snapshot.execution_policy == snapshot.execution_policy
    assert request.launch_policy_snapshot.tools == snapshot.tools
    assert request.launch_policy_snapshot.mcp_tools == snapshot.mcp_tools
    assert request.passthrough_args == snapshot.extra_args
    assert request.agent is None
    assert request.model == ""
    assert request.skills == ()


def test_primary_continue_spawn_session_ref_recovers_linked_spawn_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracked spawn-session chat refs must load the linked spawn row's snapshot."""

    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    primary_snapshot = LaunchPolicySnapshot(
        model="gpt-5.3-codex",
        harness="codex",
        agent="agent-primary",
        skills=(),
    )
    spawn_snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness="claude",
        agent="agent-spawn",
        skills=("testing-principles",),
        extra_args=("--permission-mode", "acceptEdits"),
    )
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p50",
        harness_session_id="session-primary",
        launch_policy_snapshot=primary_snapshot,
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p51",
        chat_id="c-spawn",
        owner_chat_id="c-primary",
        model=spawn_snapshot.model,
        agent=spawn_snapshot.agent or "agent-spawn",
        skills=spawn_snapshot.skills,
        harness=spawn_snapshot.harness,
        kind="child",
        prompt="child prompt",
        harness_session_id="session-spawn",
        launch_policy_snapshot=spawn_snapshot,
    )
    spawn_chat_id = session_store.start_session(
        runtime_root,
        harness="claude",
        harness_session_id="session-spawn",
        model=spawn_snapshot.model,
        chat_id="c-spawn",
        agent=spawn_snapshot.agent or "agent-spawn",
        skills=spawn_snapshot.skills,
        kind="spawn",
        spawn_id="p51",
    )

    def fail_bundle_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot replay should not re-resolve live policy")

    monkeypatch.setattr(
        "meridian.lib.launch.bundle_adapter.request_and_resolve",
        fail_bundle_resolution,
    )

    captured: dict[str, LaunchRequest] = {}

    def fake_launch_primary(
        *,
        project_root: Path,
        request: LaunchRequest,
        harness_registry: object,
    ) -> LaunchResult:
        _ = (project_root, harness_registry)
        captured["request"] = request
        return LaunchResult(
            command=(),
            exit_code=0,
            continue_ref="session-spawn",
            continue_chat_id=spawn_chat_id,
        )

    try:
        with patch("meridian.cli.primary_launch.launch_primary", side_effect=fake_launch_primary):
            run_primary_launch(
                project_root=project_root,
                continue_ref=spawn_chat_id,
                fork_ref=None,
                fork_fresh_ref=None,
                model="",
                harness=None,
                agent=None,
                work="",
                task_dir=None,
                yolo=False,
                approval=None,
                autocompact=None,
                effort=None,
                sandbox=None,
                timeout=None,
                dry_run=False,
                passthrough=(),
                skills=(),
            )
    finally:
        session_store.stop_session(runtime_root, spawn_chat_id)

    request = captured["request"]
    assert request.launch_policy_snapshot == spawn_snapshot
    assert request.passthrough_args == spawn_snapshot.extra_args


def test_primary_continue_from_source_without_work_suppresses_ambient_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    source_task_dir = tmp_path / "source-worktree"
    source_task_dir.mkdir()
    ambient_work_dir = tmp_path / "ambient-work"
    ambient_work_dir.mkdir()
    runtime_root = _state_root(project_root)
    snapshot = LaunchPolicySnapshot(model="gpt-5.3-codex", harness="codex")
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p45",
        harness_session_id="session-45",
        work_id=None,
        task_cwd=source_task_dir.as_posix(),
        launch_policy_snapshot=snapshot,
    )
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "ambient-work")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", ambient_work_dir.as_posix())

    captured_contexts: list[Any] = []

    def fake_run_harness_process(
        context: Any,
        harness_registry: object,
        **kwargs: object,
    ) -> ProcessOutcome:
        _ = (harness_registry, kwargs)
        captured_contexts.append(context)
        return ProcessOutcome(
            command=(),
            exit_code=0,
            chat_id="c45",
            primary_spawn_id="p45-continue",
            primary_started=0.0,
            primary_started_epoch=0.0,
            primary_started_local_iso=None,
            resolved_harness_session_id="session-45",
        )

    monkeypatch.setattr(
        "meridian.lib.launch.process.run_harness_process",
        fake_run_harness_process,
    )

    run_primary_launch(
        project_root=project_root,
        continue_ref="p45",
        fork_ref=None,
        fork_fresh_ref=None,
        model="",
        harness=None,
        agent=None,
        work="",
        task_dir=None,
        yolo=False,
        approval=None,
        autocompact=None,
        effort=None,
        sandbox=None,
        timeout=None,
        dry_run=False,
        passthrough=(),
        skills=(),
    )

    context = captured_contexts[0]
    assert context.work_id is None
    assert context.resolved_request.work_id_hint is None
    assert context.binding.work_id is None
    assert "MERIDIAN_ACTIVE_WORK_ID" not in context.binding.environment.child_context_env
    assert context.task_cwd == source_task_dir


def test_primary_continue_rejects_passthrough_args(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p52",
        harness_session_id="session-52",
        launch_policy_snapshot=LaunchPolicySnapshot(
            model="gpt-5.3-codex",
            harness="codex",
        ),
    )

    with pytest.raises(ValueError) as exc_info:
        run_primary_launch(
            project_root=project_root,
            continue_ref="p52",
            fork_ref=None,
            fork_fresh_ref=None,
            model="",
            harness=None,
            agent=None,
            work="",
            task_dir=None,
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=False,
            passthrough=("--custom",),
            skills=(),
        )

    assert "--" in str(exc_info.value)


def test_primary_continue_rejects_task_dir_override(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()

    with pytest.raises(ValueError) as exc_info:
        run_primary_launch(
            project_root=project_root,
            continue_ref="p52",
            fork_ref=None,
            fork_fresh_ref=None,
            model="",
            harness=None,
            agent=None,
            work="",
            task_dir=tmp_path.as_posix(),
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=False,
            passthrough=(),
            skills=(),
        )

    assert "--continue does not accept --task-dir" in str(exc_info.value)
    assert "--fork --task-dir" in str(exc_info.value)


def test_primary_continue_rejects_work_override(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()

    with pytest.raises(ValueError) as exc_info:
        run_primary_launch(
            project_root=project_root,
            continue_ref="p52",
            fork_ref=None,
            fork_fresh_ref=None,
            model="",
            harness=None,
            agent=None,
            work="other-work",
            task_dir=None,
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=False,
            passthrough=(),
            skills=(),
        )

    assert "--work" in str(exc_info.value)
    assert "--fork-fresh" in str(exc_info.value)
    assert "fresh session" in str(exc_info.value)


def test_primary_continue_replays_snapshot_extra_args_not_cli_passthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness="claude",
        agent="agent-a",
        skills=(),
        extra_args=("--permission-mode", "acceptEdits"),
    )
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p53",
        harness_session_id="session-53",
        launch_policy_snapshot=snapshot,
    )

    def fail_bundle_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot replay should not re-resolve live policy")

    monkeypatch.setattr(
        "meridian.lib.launch.bundle_adapter.request_and_resolve",
        fail_bundle_resolution,
    )

    captured: dict[str, LaunchRequest] = {}

    def fake_launch_primary(
        *,
        project_root: Path,
        request: LaunchRequest,
        harness_registry: object,
    ) -> LaunchResult:
        _ = (project_root, harness_registry)
        captured["request"] = request
        return LaunchResult(
            command=(),
            exit_code=0,
            continue_ref="session-53",
            continue_chat_id="c53",
        )

    with patch("meridian.cli.primary_launch.launch_primary", side_effect=fake_launch_primary):
        run_primary_launch(
            project_root=project_root,
            continue_ref="p53",
            fork_ref=None,
            fork_fresh_ref=None,
            model="",
            harness=None,
            agent=None,
            work="",
            task_dir=None,
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=False,
            passthrough=(),
            skills=(),
        )

    spawn_request = build_primary_spawn_request(request=captured["request"])
    assert captured["request"].passthrough_args == snapshot.extra_args
    assert spawn_request.extra_args == snapshot.extra_args
    assert spawn_request.extra_args != ("--custom",)


def test_primary_continue_uses_legacy_bundle_resolution_without_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    source_task_dir = tmp_path / "legacy-worktree"
    source_task_dir.mkdir()
    runtime_root = _state_root(project_root)
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p42",
        harness_session_id="session-42",
        work_id="legacy-work",
        task_cwd=source_task_dir.as_posix(),
        launch_policy_snapshot=None,
    )

    monkeypatch.setenv("MERIDIAN_MODEL", "claude-sonnet-4-6")
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        model_token="gpt-5.3-codex",
        harness=HarnessId.CODEX,
        harness_model="openai/gpt-5.3-codex",
    )

    captured: dict[str, LaunchRequest] = {}

    def fake_launch_primary(
        *,
        project_root: Path,
        request: LaunchRequest,
        harness_registry: object,
    ) -> LaunchResult:
        _ = (project_root, harness_registry)
        captured["request"] = request
        return LaunchResult(
            command=(),
            exit_code=0,
            continue_ref="session-42",
            continue_chat_id="c42",
        )

    with patch("meridian.cli.primary_launch.launch_primary", side_effect=fake_launch_primary):
        run_primary_launch(
            project_root=project_root,
            continue_ref="p42",
            fork_ref=None,
            fork_fresh_ref=None,
            model="",
            harness=None,
            agent=None,
            work="",
            task_dir=None,
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=False,
            passthrough=(),
            skills=(),
        )

    request = captured["request"]
    assert request.work_id == "legacy-work"
    assert request.task_dir == source_task_dir.as_posix()
    assert request.session.source_execution_cwd == source_task_dir.as_posix()
    assert request.launch_policy_snapshot is None


def test_primary_continue_snapshot_replay_preserves_agent_over_config_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache-shaping launch policy must survive --continue after config/env drift."""

    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".mars").mkdir()
    (project_root / ".mars" / "config.toml").write_text(
        '[primary]\nagent = "agent-b"\nmodel = "gpt-5.4"\n',
        encoding="utf-8",
    )
    runtime_root = _state_root(project_root)
    snapshot = LaunchPolicySnapshot(
        model="gpt-5.3-codex",
        harness="codex",
        agent="agent-a",
        skills=("skill-a",),
        loaded_skills=(
            SkillContent(
                name="skill-a",
                description="skill-a skill",
                path="/skills/skill-a/SKILL.md",
                content="# skill-a\n\nPreserve cache contract.\n",
                skill_type="reference",
            ),
        ),
        execution_policy=ResolvedExecutionPolicy(approval="auto", sandbox="workspace-write"),
        tools={"write": "allow"},
        mcp_tools=("github",),
        extra_args=("--search",),
    )
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p43",
        harness_session_id="session-43",
        launch_policy_snapshot=snapshot,
    )
    monkeypatch.setenv("MERIDIAN_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("MERIDIAN_AGENT", "agent-c")
    monkeypatch.setenv("MERIDIAN_APPROVAL", "never")
    monkeypatch.setenv("MERIDIAN_SANDBOX", "read-only")

    bundle_calls: list[object] = []

    def fail_bundle_resolution(*_args: object, **_kwargs: object) -> object:
        bundle_calls.append(True)
        raise AssertionError("snapshot replay should not call mars launch-bundle")

    monkeypatch.setattr(
        "meridian.lib.launch.bundle_adapter.request_and_resolve",
        fail_bundle_resolution,
    )

    launch_request = LaunchRequest(
        harness="codex",
        session_mode=SessionMode.RESUME,
        launch_policy_snapshot=snapshot,
        session=SessionRequest(
            requested_harness_session_id="session-43",
            continue_harness="codex",
        ),
    )
    spawn_request = build_primary_spawn_request(request=launch_request)
    runtime = build_primary_launch_runtime(project_root=project_root)
    prepared_policy = compile_prepared_policy_surface(
        request=spawn_request,
        runtime=runtime,
        project_root=project_root,
        harness_registry=get_default_harness_registry(),
        catalog=CatalogSession(project_root),
        active_work_dir=None,
        dry_run=True,
    )

    assert bundle_calls == []
    resolved_policy = prepared_policy.resolved_policy
    assert resolved_policy.model == snapshot.model
    assert str(resolved_policy.harness) == snapshot.harness
    assert resolved_policy.routing.agent == snapshot.agent
    assert resolved_policy.resolved_skills.skill_names == snapshot.skills
    assert resolved_policy.execution_policy == snapshot.execution_policy
    assert resolved_policy.resolved_tools == snapshot.tools
    assert resolved_policy.resolved_mcp_tools == snapshot.mcp_tools


def test_primary_continue_replays_codex_empty_model_snapshot_as_default_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".mars").mkdir()
    (project_root / ".mars" / "config.toml").write_text(
        '[primary]\nmodel = "gpt-5.4"\nagent = "agent-b"\n',
        encoding="utf-8",
    )
    runtime_root = _state_root(project_root)
    snapshot = LaunchPolicySnapshot(
        model="",
        harness="codex",
        agent="tech-lead",
        skills=(),
        execution_policy=ResolvedExecutionPolicy(approval="default", sandbox="workspace-write"),
    )
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p44",
        harness_session_id="session-44",
        launch_policy_snapshot=snapshot,
    )
    monkeypatch.setenv("MERIDIAN_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("MERIDIAN_AGENT", "agent-c")

    def fail_bundle_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("empty-model snapshot replay should not call mars launch-bundle")

    monkeypatch.setattr(
        "meridian.lib.launch.bundle_adapter.request_and_resolve",
        fail_bundle_resolution,
    )

    captured: dict[str, LaunchRequest] = {}

    def fake_launch_primary(
        *,
        project_root: Path,
        request: LaunchRequest,
        harness_registry: object,
    ) -> LaunchResult:
        _ = (project_root, harness_registry)
        captured["request"] = request
        return LaunchResult(command=("codex",), exit_code=0, continue_ref="p44")

    with patch("meridian.cli.primary_launch.launch_primary", side_effect=fake_launch_primary):
        run_primary_launch(
            project_root=project_root,
            continue_ref="p44",
            fork_ref=None,
            fork_fresh_ref=None,
            model="",
            harness=None,
            agent=None,
            work="",
            task_dir=None,
            yolo=False,
            approval=None,
            autocompact=None,
            effort=None,
            sandbox=None,
            timeout=None,
            dry_run=False,
            passthrough=(),
            skills=(),
        )

    launch_request = captured["request"]
    assert launch_request.launch_policy_snapshot == snapshot

    ctx = build_launch_context(
        spawn_id="p44-replay",
        request=build_primary_spawn_request(request=launch_request),
        runtime=build_primary_launch_runtime(project_root=project_root),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert ctx.resolved_request.launch_policy_snapshot == snapshot
    assert ctx.resolved_request.model == ""
    assert ctx.resolved_request.harness == "codex"
    assert ctx.resolved_request.agent == "tech-lead"
    assert ctx.model_selection is None
    assert ctx.binding.run_params.model is None
    assert ctx.binding.spec.model is None
    assert "--model" not in ctx.binding.argv
