from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.config.loader import resolve_allow_builtin_agents_for_launch
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_appserver_command,
)
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.request import LaunchCompositionSurface, LaunchRuntime, SpawnRequest
from tests.support.launch import build_launch_context, stub_bundle_request_and_resolve

_DISABLED = ["-c", "features.multi_agent_v2=false", "-c", "agents.max_depth=0"]


def _appserver_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """The `codex app-server` command for a launch bound in ``tmp_path``'s project."""

    stub_bundle_request_and_resolve(monkeypatch, model="gpt-5.4", harness=HarnessId.CODEX)
    context = build_launch_context(
        spawn_id="p-codex-builtin",
        request=SpawnRequest(
            prompt="x", model="gpt-5.4", harness=HarnessId.CODEX.value, extra_args=extra_args
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
    return project_codex_spec_to_appserver_command(context.binding.spec, host="127.0.0.1", port=1)


def _empty(path: Path) -> str:
    path.write_text("")
    return path.as_posix()


def _contains(command: list[str], flags: list[str]) -> bool:
    return any(command[i : i + len(flags)] == flags for i in range(len(command)))


@pytest.mark.parametrize("toml, expected", [("", False), ("allow_builtin_agents = true\n", True)])
def test_codex_builtin_agents_follow_harness_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, toml: str, expected: bool
) -> None:
    monkeypatch.setenv("MERIDIAN_CONFIG", _empty(tmp_path / "user.toml"))
    (tmp_path / "meridian.toml").write_text(f"[harness.codex]\n{toml}")

    allowed = resolve_allow_builtin_agents_for_launch(
        harness_id="codex", config_snapshot=None, project_root=tmp_path
    )
    assert allowed is expected
    # The setting is per harness: enabling Codex does not enable Claude.
    assert (
        resolve_allow_builtin_agents_for_launch(
            harness_id="claude", config_snapshot=None, project_root=tmp_path
        )
        is False
    )

    command = _appserver_command(tmp_path, monkeypatch)
    assert _contains(command, _DISABLED) is not expected


@pytest.mark.parametrize(
    "default, override",
    [
        ("agents.max_depth=0", "agents.max_depth=1"),
        ("features.multi_agent_v2=false", "features.multi_agent_v2=true"),
    ],
)
def test_codex_passthrough_override_follows_default_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default: str, override: str
) -> None:
    # Codex applies `-c` overrides in order, so a later passthrough value wins.
    monkeypatch.setenv("MERIDIAN_CONFIG", _empty(tmp_path / "user.toml"))
    command = _appserver_command(tmp_path, monkeypatch, extra_args=("-c", override))

    assert command.index(override) > command.index(default)
