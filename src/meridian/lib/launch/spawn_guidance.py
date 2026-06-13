"""Spawn-capability gating and harness-templated spawn usage contracts."""

from __future__ import annotations

from meridian.lib.catalog.agent import AgentProfile
from meridian.lib.core.types import HarnessId

_CLAUDE_SPAWN_CONTRACT = """\
# Spawning subagents (meridian)

Launch detached, then wait — never block the turn or background-wrap.

- Launch with --bg; it returns a spawn id without waiting for the work and
  runs the worker detached:
      meridian spawn -a <agent> --prompt-file /tmp/<task>.md --bg
- NEVER wrap `meridian spawn --bg` inside Bash's run_in_background. It
  already detaches; double-backgrounding risks the launch being killed
  before the spawn is recorded.
- NEVER block-foreground a long spawn (omitting --bg). It will outlive your
  Bash command timeout; you lose the thread while the spawn runs on.
- Track with no-arg wait — no id needed; it discovers your pending spawns
  by session and yields cache-cleanly. Re-invoke to keep waiting:
      meridian spawn wait
- Full reference:  meridian spawn --help"""

_GENERIC_SPAWN_CONTRACT = """\
# Spawning subagents (meridian)

Launch detached, then wait — never block the turn or double-background.

- Launch with --bg; it returns a spawn id without waiting for the work and
  runs the worker detached:
      meridian spawn -a <agent> --prompt-file /tmp/<task>.md --bg
- NEVER wrap `meridian spawn --bg` in your harness's background execution.
  It already detaches; double-backgrounding risks the launch being killed
  before the spawn is recorded.
- NEVER block-foreground a long spawn (omitting --bg). It will outlive your
  command timeout; you lose the thread while the spawn runs on.
- Track with no-arg wait — no id needed; it discovers your pending spawns
  by session and yields cache-cleanly. Re-invoke to keep waiting:
      meridian spawn wait
- Full reference:  meridian spawn --help"""


def _spawn_usage_contract(harness: HarnessId) -> str:
    if harness == HarnessId.CLAUDE:
        return _CLAUDE_SPAWN_CONTRACT
    return _GENERIC_SPAWN_CONTRACT


def has_spawn_capability(profile: AgentProfile | None) -> bool:
    if profile is None:
        return False
    if profile.meridian_capabilities is not None and profile.meridian_capabilities.spawn is False:
        return False
    return len(profile.subagents) > 0


def resolve_spawn_prompt_blocks(
    *,
    profile: AgentProfile | None,
    harness_id: HarnessId,
    bundle_inventory_prompt: str | None,
) -> tuple[str, str]:
    """Return (inventory_block, spawn_contract_block).

    Both empty strings when the agent is not spawn-capable.
    """
    if not has_spawn_capability(profile):
        return "", ""
    inventory_block = (bundle_inventory_prompt or "").strip()
    return inventory_block, _spawn_usage_contract(harness_id)


__all__ = [
    "has_spawn_capability",
    "resolve_spawn_prompt_blocks",
]
