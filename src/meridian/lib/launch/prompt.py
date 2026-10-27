"""Prompt composition helpers for launch flows."""

import html


def build_report_instruction() -> str:
    """Build the report instruction appended to each composed run prompt."""

    return (
        "# Report\n\n"
        "**IMPORTANT - Your final assistant message must be the run report.**\n\n"
        "Provide a plain markdown report in your final assistant message.\n\n"
        "Include: what was done, key decisions made, files created/modified, "
        "verification results, and any issues or blockers."
    )


def build_goal_instruction(goal: str | None) -> str:
    """Render the deterministic goal completion contract block.

    One text for both surfaces: a primary launch with a goal and a spawn with a goal
    hold the same contract, and each composer places the block itself — a spawn in
    its task turn, a primary in its system prompt.
    """

    if goal is None:
        return ""
    if goal == "" or goal != goal.strip():
        raise ValueError("goal must be normalized before prompt rendering")
    escaped_goal = html.escape(goal, quote=False)
    return (
        "# Goal\n\n"
        "You have a completion contract for this run:\n\n"
        f"<goal>\n{escaped_goal}\n</goal>\n\n"
        "Work until the goal is complete. If the goal is impossible, unsafe, "
        "blocked by missing information or permissions, or disproportionate "
        "to continue, stop and report the blocker instead.\n\n"
        "When blocked, report what is blocked, the evidence observed, and the "
        "smallest next action or decision needed. Do not run forever or retry indefinitely."
    )


def build_spawn_preamble() -> str:
    """Render behavioral framing for spawned subagents.

    Foreground and background share one text: continuing a background spawn in the
    foreground must not change the system prompt, and both are the same situation —
    the caller is blocked on the report, so escalation means reporting back.
    """

    return (
        "# Session Context\n\n"
        "This is a **sub-agent session**. You are not talking to the user. "
        "Your final output is a structured report consumed by your parent agent. "
        "Work autonomously toward your objective. Only escalate if blocked."
    )


def build_primary_preamble() -> str:
    """Render session-context framing for primary (user-facing) sessions."""

    return (
        "# Session Context\n\n"
        "This is a **primary session**. You are talking directly to the user."
    )
