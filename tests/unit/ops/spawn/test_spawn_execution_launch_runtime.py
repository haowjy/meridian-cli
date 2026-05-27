"""Spawn Mars runtime: SPAWN_PREPARE surface and harness_model on execute."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.composition_spawn import bind_spawn_launch_context
from meridian.lib.launch.context import RuntimeBindings
from meridian.lib.launch.plan import build_spawn_mars_runtime
from meridian.lib.launch.request import LaunchArgvIntent, LaunchCompositionSurface
from meridian.lib.ops.runtime import build_runtime
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.ops.spawn.prepare import build_create_payload
from tests.support.executables import prepend_fake_executables
from tests.support.launch import stub_bundle_request_and_resolve

_HARNESS_CASES: tuple[tuple[str, str, str, HarnessId], ...] = (
    ("pi", "openai-codex/gpt-5.4-mini", "openai-codex/gpt-5.4-mini", HarnessId.PI),
    ("cursor", "opus47", "claude-opus-4-7-thinking-high", HarnessId.CURSOR),
    ("opencode", "gpt-5.5", "openai/gpt-5.5", HarnessId.OPENCODE),
)


def _seed_project(tmp_path: Path) -> Path:
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("harness", "cli_model", "stub_harness_model", "harness_id"),
    _HARNESS_CASES,
    ids=[case[0] for case in _HARNESS_CASES],
)
def test_spawn_execute_uses_harness_model_from_mars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    cli_model: str,
    stub_harness_model: str,
    harness_id: HarnessId,
) -> None:
    project_root = _seed_project(tmp_path)
    prepend_fake_executables(monkeypatch, tmp_path, harness)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model=cli_model,
        harness=harness_id,
        harness_model=stub_harness_model,
    )
    runtime = build_runtime(project_root)
    runtime_root = project_root / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)

    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="hi",
            model=cli_model,
            harness=harness,
            project_root=str(project_root),
        ),
        runtime=runtime,
    )
    launch_runtime = build_spawn_mars_runtime(
        runtime=runtime,
        runtime_root=runtime_root,
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
    )
    assert launch_runtime.composition_surface is LaunchCompositionSurface.SPAWN_PREPARE
    assert launch_runtime.config_snapshot

    ctx = bind_spawn_launch_context(
        prepared=artifacts.prepared,
        bindings=RuntimeBindings(
            spawn_id="p-exec",
            report_output_path=project_root / "report.md",
            dry_run=False,
        ),
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
    )
    assert ctx.binding.spec.model == stub_harness_model


@pytest.mark.parametrize(
    ("harness", "cli_model", "stub_harness_model", "harness_id"),
    _HARNESS_CASES,
    ids=[f"prepare-execute-{case[0]}" for case in _HARNESS_CASES],
)
def test_spawn_prepare_then_execute_binding_spec_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    cli_model: str,
    stub_harness_model: str,
    harness_id: HarnessId,
) -> None:
    project_root = _seed_project(tmp_path)
    prepend_fake_executables(monkeypatch, tmp_path, harness)
    captured = stub_bundle_request_and_resolve(
        monkeypatch,
        model=cli_model,
        harness=harness_id,
        harness_model=stub_harness_model,
    )
    runtime = build_runtime(project_root)
    runtime_root = project_root / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)

    artifacts = build_create_payload(
        SpawnCreateInput(
            prompt="hi",
            model=cli_model,
            harness=harness,
            project_root=str(project_root),
        ),
        runtime=runtime,
    )
    assert len(captured) == 1
    execute_runtime = build_spawn_mars_runtime(
        runtime=runtime,
        runtime_root=runtime_root,
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
    )
    ctx = bind_spawn_launch_context(
        prepared=artifacts.prepared,
        bindings=RuntimeBindings(
            spawn_id="p-exec",
            report_output_path=project_root / "report.md",
            dry_run=False,
        ),
        runtime=execute_runtime,
        harness_registry=get_default_harness_registry(),
    )
    assert len(captured) == 1
    assert ctx.binding.spec.model == stub_harness_model
