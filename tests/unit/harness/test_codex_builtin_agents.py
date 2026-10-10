from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.config.settings import resolve_allow_builtin_agents_for_launch
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.projections.project_codex_streaming import (
    project_codex_spec_to_appserver_command,
)
from meridian.lib.safety.permissions import PermissionConfig, TieredPermissionResolver

_DISABLED = ["-c", "features.multi_agent_v2=false", "-c", "agents.max_depth=0"]


def _appserver_command(params: SpawnParams) -> list[str]:
    spec = CodexAdapter().resolve_launch_spec(
        params, TieredPermissionResolver(config=PermissionConfig())
    )
    return project_codex_spec_to_appserver_command(spec, host="127.0.0.1", port=1)


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

    command = _appserver_command(SpawnParams(prompt="x", allow_builtin_agents=allowed))
    assert _contains(command, _DISABLED) is not expected


@pytest.mark.parametrize(
    "default, override",
    [
        ("agents.max_depth=0", "agents.max_depth=1"),
        ("features.multi_agent_v2=false", "features.multi_agent_v2=true"),
    ],
)
def test_codex_passthrough_override_follows_default_disable(default: str, override: str) -> None:
    # Codex applies `-c` overrides in order, so a later passthrough value wins.
    command = _appserver_command(SpawnParams(prompt="x", extra_args=("-c", override)))

    assert command.index(override) > command.index(default)
