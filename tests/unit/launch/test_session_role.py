from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest
from tests.support.fixtures import allow_headless_claude
from tests.support.launch import stub_bundle_request_and_resolve


@pytest.mark.parametrize(
    ("surface", "inherited_role", "expected_role"),
    [
        (LaunchCompositionSurface.PRIMARY, None, "primary"),
        (LaunchCompositionSurface.SPAWN_PREPARE, None, "spawn"),
        (LaunchCompositionSurface.DIRECT, None, "spawn"),
        (LaunchCompositionSurface.DIRECT, "primary", "spawn"),
    ],
)
def test_bind_sets_session_role_from_composition_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: LaunchCompositionSurface,
    inherited_role: str | None,
    expected_role: str,
) -> None:
    allow_headless_claude(tmp_path)
    (tmp_path / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="haiku",
        harness=HarnessId.CLAUDE,
    )
    if inherited_role is None:
        monkeypatch.delenv("MERIDIAN_SESSION_ROLE", raising=False)
    else:
        monkeypatch.setenv("MERIDIAN_SESSION_ROLE", inherited_role)

    context = build_launch_context(
        spawn_id="p-session-role",
        request=SpawnRequest(
            prompt="test",
            model="haiku",
            harness=HarnessId.CLAUDE.value,
        ),
        runtime=LaunchRuntime(
            composition_surface=surface,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert context.binding.environment.child_context_env["MERIDIAN_SESSION_ROLE"] == expected_role
    assert context.binding.environment.final_env["MERIDIAN_SESSION_ROLE"] == expected_role
