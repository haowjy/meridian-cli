# qa-validated: test-suite-redesign
"""Tests for build_launch_context — context construction, goal injection, surface behavior."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.composition import PromptDocument
from meridian.lib.launch.constants import DRY_RUN_REPORT_PATH, REPORT_FILENAME
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)
from meridian.lib.state.paths import resolve_spawn_log_dir
from tests.support.fixtures import allow_headless_claude
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
        runtime_root=(tmp_path / ".meridian").as_posix(),
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=resolved_execution_cwd.as_posix(),
    )


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )
    allow_headless_claude(project_root)


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
            False,
            id="raw-request-runtime-bind-argv",
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


def test_build_launch_context_dry_run_codex_o_uses_sentinel(tmp_path: Path) -> None:
    request = _build_spawn_request()
    runtime = _build_launch_runtime(tmp_path=tmp_path)
    registry = get_default_harness_registry()
    spawn_id = "p-ctx"

    runtime_ctx = build_launch_context(
        spawn_id=spawn_id,
        request=request,
        runtime=runtime,
        harness_registry=registry,
        dry_run=False,
    )
    dry_run_ctx = build_launch_context(
        spawn_id=spawn_id,
        request=request,
        runtime=runtime,
        harness_registry=registry,
        dry_run=True,
    )

    runtime_o = runtime_ctx.binding.argv.index("-o")
    dry_o = dry_run_ctx.binding.argv.index("-o")
    expected_report = resolve_spawn_log_dir(tmp_path, spawn_id) / REPORT_FILENAME
    assert runtime_ctx.binding.argv[runtime_o + 1] == expected_report.as_posix()
    assert dry_run_ctx.binding.argv[dry_o + 1] == DRY_RUN_REPORT_PATH


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


def test_collect_context_projection_roots_skips_missing_dirs(tmp_path: Path) -> None:
    """Context roots that don't exist must not be projected into the sandbox.

    Sandboxed harnesses (codex bubblewrap) bind-mount every projected root and
    abort all command execution if a source path is missing. A not-yet-created
    context dir (commonly ``work_archive`` before the first archive) must be
    skipped rather than handed to the sandbox.
    """
    from meridian.lib.config.context_config import ContextConfig
    from meridian.lib.context.resolver import resolve_context_paths
    from meridian.lib.launch.context import _collect_context_projection_roots

    project_root = tmp_path / "proj"
    project_root.mkdir()
    config = ContextConfig()
    resolved = resolve_context_paths(project_root, config)

    # Fresh project: none of work_root / work_archive / kb_root exist yet.
    assert _collect_context_projection_roots(project_root, config) == ()

    # Create only work_root; it alone should be projected.
    resolved.work_root.mkdir(parents=True, exist_ok=True)
    assert _collect_context_projection_roots(project_root, config) == (resolved.work_root,)
