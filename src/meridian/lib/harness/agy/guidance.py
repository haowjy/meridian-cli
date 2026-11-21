"""The Meridian guidance an ``agy`` session gets from its hook, built by the launch builders.

Other harnesses receive this text in their launch prompt; ``agy`` has no launch, so the hook
delivers it: the static part at ``SessionStart``, and the session's own facts (chat ID,
links) once the server holds its row. Imports are local: only a hook that has something to
say pays for the launch stack.

The hook payload names no agent, so there is no profile to read. The session is treated as
able to spawn: it was given the tool for that, and ``meridian spawn subagents`` lists what
it may start.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

_WAIT_RULES = """\
- Launch spawns with --bg and collect their results with `meridian spawn wait`; with no
  arg it finds your running and owed spawns. A tool call blocks your turn until it
  returns and the tool has no background mode, so launch everything first, then wait once.
  Do not pipe or truncate its output."""


def static_guidance(cwd: Path) -> str:
    """Skills index, capabilities without per-session IDs, spawn rules and prompting."""

    from meridian.lib.config.project_root import resolve_project_root_resolution
    from meridian.lib.harness.adapter import SpawnUsageContractVariants
    from meridian.lib.launch.composition import (
        CAPABILITIES_BLOCK_NAME,
        render_guidance_blocks,
    )
    from meridian.lib.launch.spawn_guidance import (
        build_capabilities_prompt,
        build_guidance_blocks,
        build_spawn_usage_contract,
    )

    project_root = resolve_project_root_resolution(execution_cwd=cwd, ignore_env=True).project_root
    contract = build_spawn_usage_contract(
        SpawnUsageContractVariants(
            intro_line="Launch detached, then wait once.",
            double_wrap_bullet=(
                "`meridian spawn --bg` detaches itself: call it directly; the tool has no\n"
                "  background mode to wrap it in."
            ),
            timeout_bullet=(
                "NEVER omit --bg for a long spawn: the call blocks your turn until it ends."
            ),
        ),
        kind="primary",
        wait_rules=_WAIT_RULES,
    )
    capabilities = build_capabilities_prompt(
        spawn_capable=True,
        project_root=project_root,
        control_root=project_root,
        task_dir=project_root,
        via_tool=True,
    )
    blocks = tuple(
        replace(block, content=capabilities) if block.name == CAPABILITIES_BLOCK_NAME else block
        for block in build_guidance_blocks(
            spawn_capable=True,
            lead=True,
            loaded_skills=frozenset(),
            spawn_usage_contract=contract,
        )
    )
    return render_guidance_blocks(blocks)


def session_facts(row: dict[str, str]) -> str:
    """The spawn ID, chat ID and links of a row the server holds (``holds.py``)."""

    from meridian.lib.launch.session_links import session_task_link_path, session_work_link_path
    from meridian.lib.launch.spawn_guidance import build_session_facts_prompt

    runtime_root = Path(row["runtime_root"])
    chat_id = row["chat_id"]
    return build_session_facts_prompt(
        spawn_id=row["spawn_id"],
        chat_id=chat_id,
        project_root=Path(row["project_root"]),
        work_link=session_work_link_path(runtime_root, chat_id),
        task_link=session_task_link_path(runtime_root, chat_id),
    )
