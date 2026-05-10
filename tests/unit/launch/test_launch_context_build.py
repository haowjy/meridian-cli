# qa-validated: test-suite-redesign
"""Tests for build_launch_context — context construction, goal injection, surface behavior."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.composition import PromptDocument
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)

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
        "[settings]\n"
        'targets = [".claude"]\n',
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
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
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
