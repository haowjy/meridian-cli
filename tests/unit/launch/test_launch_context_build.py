# qa-validated: test-suite-redesign
"""Tests for build_launch_context — context construction, goal injection, surface behavior."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.bundle_adapter import LoadedSkillEntry
from meridian.lib.launch.composition import PromptDocument
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from tests.support.fixtures import write_agent
from tests.support.launch import stub_bundle_request_and_resolve

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _build_spawn_request(
    prompt: str = "hello",
    extra_args: tuple[str, ...] = (),
    goal: str | None = None,
) -> SpawnRequest:
    return SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt=prompt,
        extra_args=extra_args,
        goal=goal,
    )


def _build_primary_spawn_request(
    *,
    supplemental_prompt_documents: tuple[PromptDocument, ...] = (),
) -> SpawnRequest:
    return SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt="# Meridian Session",
        supplemental_prompt_documents=supplemental_prompt_documents,
    )


def _build_launch_runtime(
    *,
    tmp_path: Path,
    argv_intent: LaunchArgvIntent = LaunchArgvIntent.REQUIRED,
    composition_surface: LaunchCompositionSurface = LaunchCompositionSurface.DIRECT,
    execution_cwd: Path | None = None,
) -> LaunchRuntime:
    resolved_execution_cwd = execution_cwd or tmp_path
    return LaunchRuntime(
        argv_intent=argv_intent,
        composition_surface=composition_surface,
        report_output_path=(tmp_path / "report.md").as_posix(),
        runtime_root=(tmp_path / ".meridian").as_posix(),
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=resolved_execution_cwd.as_posix(),
    )


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    (
        "extra_args",
        "argv_intent",
        "patch_argv_failure",
        "expected_argv",
        "compare_dry_run",
    ),
    [
        pytest.param(
            (),
            LaunchArgvIntent.REQUIRED,
            False,
            None,
            True,
            id="raw-request-runtime-dry-run-share-argv",
        ),
        pytest.param(
            (),
            LaunchArgvIntent.SPEC_ONLY,
            True,
            (),
            False,
            id="spec-only-tolerates-argv-build-failure",
        ),
    ],
)
def test_build_launch_context_behaviors(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    extra_args: tuple[str, ...],
    argv_intent: LaunchArgvIntent,
    patch_argv_failure: bool,
    expected_argv: tuple[str, ...] | None,
    compare_dry_run: bool,
) -> None:
    request = _build_spawn_request(extra_args=extra_args)
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        argv_intent=argv_intent,
    )
    registry = get_default_harness_registry()

    if patch_argv_failure:

        def fail_build_launch_argv(**_: object) -> tuple[str, ...]:
            raise RuntimeError("argv unavailable")

        monkeypatch.setattr(
            "meridian.lib.launch.context.build_launch_argv",
            fail_build_launch_argv,
        )

    runtime_ctx = build_launch_context(
        spawn_id="p-ctx",
        request=request,
        runtime=runtime,
        harness_registry=registry,
        dry_run=False,
    )

    if compare_dry_run:
        dry_run_ctx = build_launch_context(
            spawn_id="p-ctx",
            request=request,
            runtime=runtime,
            harness_registry=registry,
            dry_run=True,
        )
        assert runtime_ctx.binding.argv == dry_run_ctx.binding.argv

    if expected_argv is not None:
        assert runtime_ctx.binding.argv == expected_argv

    if patch_argv_failure:
        assert runtime_ctx.binding.spec is not None


def test_build_launch_context_primary_projects_supplemental_documents(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    request = _build_primary_spawn_request(
        supplemental_prompt_documents=(
            PromptDocument(
                kind="bootstrap",
                logical_name="setup",
                path="/setup/BOOTSTRAP.md",
                content="# Bootstrap: setup\n\nsetup docs",
            ),
        )
    )
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-docs",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    appended = runtime_ctx.binding.run_params.appended_system_prompt
    assert "# Bootstrap: setup\n\nsetup docs" in appended


def test_build_launch_context_spawn_prepare_injects_goal_completion_contract(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    request = _build_spawn_request(goal="finish launch composition wiring").model_copy(
        update={"prompt_is_composed": False}
    )
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-goal",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.resolved_request.goal == "finish launch composition wiring"
    projected_goal_contract = "\n".join(
        channel
        for channel in (
            runtime_ctx.binding.run_params.appended_system_prompt or "",
            runtime_ctx.binding.run_params.user_turn_content or "",
        )
        if channel
    )
    assert "# Spawn Goal" in projected_goal_contract
    assert "<goal>\nfinish launch composition wiring\n</goal>" in projected_goal_contract


def test_primary_bundle_auto_approval_projects_claude_accept_edits(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE,
        execution_policy=ResolvedExecutionPolicy(approval="auto"),
    )
    request = SpawnRequest(
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE.value,
        prompt="# Meridian Session",
    )
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-auto-approval",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.resolved_request.execution_policy.approval == "auto"
    assert runtime_ctx.resolved_request.launch_policy_snapshot is not None
    assert (
        runtime_ctx.resolved_request.launch_policy_snapshot.model_selection_harness_model_id
        == "claude-sonnet-4-6"
    )
    assert "--permission-mode" in runtime_ctx.binding.argv
    assert "acceptEdits" in runtime_ctx.binding.argv


def test_primary_launch_policy_snapshot_persists_loaded_skills(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    write_agent(
        tmp_path,
        name="coder",
        model="gpt-5.4",
        skills=("testing-principles",),
        harness=HarnessId.CODEX.value,
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
        skills_loaded=(
            LoadedSkillEntry(
                name="testing-principles",
                skill_type="principle",
                body="# testing-principles\n\nBe consistent.",
            ),
        ),
    )
    request = SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        agent="coder",
        prompt="# Meridian Session",
    )
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-snapshot-skills",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    snapshot = runtime_ctx.resolved_request.launch_policy_snapshot
    assert snapshot is not None
    assert snapshot.skills == ("testing-principles",)
    # Skill path is synthetic (from bundle, not disk)
    assert snapshot.skill_paths
    assert ".mars/skills/testing-principles/SKILL.md" in snapshot.skill_paths[0]
    assert snapshot.loaded_skills[0].name == "testing-principles"
    assert snapshot.loaded_skills[0].content == "# testing-principles\n\nBe consistent."
    assert snapshot.loaded_skills[0].skill_type == "principle"


def test_direct_launch_context_synthesizes_policy_snapshot(tmp_path: Path) -> None:
    runtime_ctx = build_launch_context(
        spawn_id="p-direct-snapshot",
        request=SpawnRequest(
            model="gpt-5.4",
            harness=HarnessId.CODEX.value,
            prompt="direct prompt",
        ),
        runtime=_build_launch_runtime(tmp_path=tmp_path),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    snapshot = runtime_ctx.resolved_request.launch_policy_snapshot
    assert snapshot is not None
    assert runtime_ctx.request.launch_policy_snapshot == snapshot
    assert snapshot.model == "gpt-5.4"
    assert snapshot.harness == HarnessId.CODEX.value


def test_spawn_prepare_reuses_policy_snapshot_over_live_env(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE.value,
        execution_policy=ResolvedExecutionPolicy(approval="auto", sandbox="workspace-write"),
    )
    monkeypatch.setenv("MERIDIAN_MODEL", "gpt-5.4")
    monkeypatch.setenv("MERIDIAN_APPROVAL", "confirm")

    def fail_bundle(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot replay should not call launch-bundle")

    monkeypatch.setattr("meridian.lib.launch.bundle_adapter.request_and_resolve", fail_bundle)
    request = SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt="snapshot replay prompt",
        prompt_is_composed=False,
        launch_policy_snapshot=snapshot,
    )
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-snapshot-replay",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.resolved_request.model == snapshot.model
    assert runtime_ctx.resolved_request.harness == snapshot.harness
    assert runtime_ctx.resolved_request.execution_policy.approval == "auto"
    assert "--permission-mode" in runtime_ctx.binding.argv
    assert "acceptEdits" in runtime_ctx.binding.argv


def test_primary_resume_includes_agent_inventory(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE,
        prompt_surface_inventory_prompt=(
            "# Meridian Agents\n\n"
            "## Subagent\n"
            "- `meridian spawn -a reviewer`: Review work."
        ),
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-resume-inventory",
        request=SpawnRequest(
            model="claude-sonnet-4-6",
            harness=HarnessId.CLAUDE.value,
            prompt="# Meridian Session",
            session=SessionRequest(primary_session_mode="resume"),
        ),
        runtime=_build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.PRIMARY,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.projected_content is not None
    system_prompt = runtime_ctx.projected_content.system_prompt
    assert "# Meridian Agents" in system_prompt
    assert "`meridian spawn -a reviewer`" in system_prompt


def test_spawn_prepare_replays_persisted_inventory_from_snapshot(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    inventory = (
        "# Meridian Agents\n\n"
        "## Subagent\n"
        "- `meridian spawn -a reviewer`: Review work."
    )
    snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness=HarnessId.CLAUDE.value,
        execution_policy=ResolvedExecutionPolicy(approval="auto", sandbox="workspace-write"),
        bundle_inventory_prompt=inventory,
    )

    def fail_bundle(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot replay should not call launch-bundle")

    monkeypatch.setattr("meridian.lib.launch.bundle_adapter.request_and_resolve", fail_bundle)
    request = SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt="snapshot replay prompt",
        prompt_is_composed=False,
        launch_policy_snapshot=snapshot,
    )
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-snapshot-inventory-replay",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.projected_content is not None
    assert inventory in runtime_ctx.projected_content.system_prompt
