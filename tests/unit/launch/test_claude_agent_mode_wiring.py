"""The profile's mode reaches the Claude launch environment on the real bind path."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest
from tests.support.fixtures import allow_headless_claude, write_agent
from tests.support.launch import build_launch_context, stub_bundle_request_and_resolve

if TYPE_CHECKING:
    from pytest import MonkeyPatch

_TTL_ENV = "CLAUDE_CODE_PROMPT_CACHE_TTL"


@pytest.fixture(autouse=True)
def _allow_headless(tmp_path: Path) -> None:
    allow_headless_claude(tmp_path)


@pytest.mark.parametrize(
    ("mode", "expected_ttl"),
    [("primary", None), ("subagent", "5m")],
)
def test_profile_mode_reaches_claude_launch_env(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    *,
    mode: str,
    expected_ttl: str | None,
) -> None:
    stub_bundle_request_and_resolve(monkeypatch, model="haiku", harness=HarnessId.CLAUDE)
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )
    write_agent(tmp_path, name=f"lead-{mode}", model="haiku", mode=mode)

    context = build_launch_context(
        spawn_id="p-cache-mode-wiring",
        request=SpawnRequest(
            prompt="test",
            model="haiku",
            harness=HarnessId.CLAUDE.value,
            agent=f"lead-{mode}",
        ),
        runtime=LaunchRuntime(
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert context.binding.spec.agent_mode == mode
    assert context.binding.environment.final_env.get(_TTL_ENV) == expected_ttl
