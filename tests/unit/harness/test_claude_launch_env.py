"""Claude's forced launch environment, per agent lifetime."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog.agent import AgentMode
from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

_TTL_ENV = "CLAUDE_CODE_PROMPT_CACHE_TTL"


@pytest.mark.parametrize(
    ("interactive", "agent_mode", "expected_ttl"),
    [
        # A primary-mode agent waits on children past five minutes, so it keeps its TTL.
        (False, "primary", None),
        (False, "subagent", "5m"),
        (False, None, "5m"),
        (True, "subagent", None),
    ],
)
def test_prompt_cache_ttl_override_follows_agent_lifetime(
    interactive: bool,
    agent_mode: AgentMode | None,
    expected_ttl: str | None,
) -> None:
    env = ClaudeAdapter().project_launch_env(
        spec=ResolvedLaunchSpec(
            interactive=interactive,
            agent_mode=agent_mode,
            agent_name="design-lead",
        ),
        system_prompt_path=Path("/launch/system-prompt.md"),
        env={},
    )

    assert env.get(_TTL_ENV) == expected_ttl
