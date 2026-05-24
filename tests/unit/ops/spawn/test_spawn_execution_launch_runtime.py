"""Spawn execution must reuse SPAWN_PREPARE routing (harness_model), not DIRECT passthrough."""

from __future__ import annotations

from pathlib import Path

from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import LaunchCompositionSurface, SpawnRequest
from meridian.lib.ops.runtime import build_runtime
from meridian.lib.ops.spawn.execute_init import build_spawn_execution_launch_runtime
from meridian.lib.ops.spawn.prepare import build_create_payload
from meridian.lib.ops.spawn.models import SpawnCreateInput


def test_spawn_execution_launch_runtime_uses_spawn_prepare_surface(tmp_path: Path) -> None:
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    runtime = build_runtime(tmp_path)
    prepared = build_create_payload(
        SpawnCreateInput(
            prompt="hi",
            model="openai-codex/gpt-5.4-mini",
            harness="pi",
            project_root=str(tmp_path),
        ),
        runtime=runtime,
    )
    launch_runtime = build_spawn_execution_launch_runtime(
        runtime=runtime,
        runtime_root=tmp_path / ".meridian",
        control_root=tmp_path,
        execution_cwd=tmp_path.as_posix(),
    )
    assert launch_runtime.composition_surface is LaunchCompositionSurface.SPAWN_PREPARE
    assert launch_runtime.config_snapshot

    ctx = build_launch_context(
        spawn_id="p-exec",
        request=prepared,
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
    )
    assert ctx.binding.spec.model == "openai-codex/gpt-5.4-mini"
