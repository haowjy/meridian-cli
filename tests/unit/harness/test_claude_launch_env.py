"""Claude's forced launch environment, per agent lifetime."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

_TTL_ENV = "CLAUDE_CODE_PROMPT_CACHE_TTL"


@pytest.mark.parametrize(
    ("interactive", "primary_mode", "expected_ttl"),
    [
        # A primary-mode agent waits on children past five minutes, so it keeps its TTL.
        (False, True, None),
        (False, False, "5m"),
        (True, False, None),
    ],
)
def test_prompt_cache_ttl_override_follows_agent_lifetime(
    interactive: bool,
    primary_mode: bool,
    expected_ttl: str | None,
) -> None:
    env = ClaudeAdapter().project_launch_env(
        spec=ResolvedLaunchSpec(interactive=interactive),
        system_prompt_path=Path("/launch/system-prompt.md"),
        env={},
        primary_mode=primary_mode,
    )

    assert env.get(_TTL_ENV) == expected_ttl
