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
    """Render the deterministic spawn-goal completion contract block."""

    if goal is None:
        return ""
    if goal == "" or goal != goal.strip():
        raise ValueError("goal must be normalized before prompt rendering")
    escaped_goal = html.escape(goal, quote=False)
    return (
        "# Spawn Goal\n\n"
        "You have a completion contract for this spawn:\n\n"
        f"<goal>\n{escaped_goal}\n</goal>\n\n"
        "Work until the goal is complete. If the goal is impossible, unsafe, "
        "blocked by missing information or permissions, or disproportionate "
        "to continue, stop and report the blocker instead.\n\n"
        "When blocked, report what is blocked, the evidence observed, and the "
        "smallest next action or decision needed. Do not run forever or retry indefinitely."
    )


def build_spawn_preamble(launch_mode: str | None) -> str:
    """Render launch-mode behavioral framing for spawned subagents."""

    if launch_mode == "background":
        return (
            "# Session Context\n\n"
            "This is a **sub-agent session**. You are not talking to the user. "
            "Your final output is a structured report consumed by your parent agent. "
            "Work autonomously toward your objective. Only escalate if blocked."
        )
    if launch_mode == "foreground":
        return (
            "# Session Context\n\n"
            "This is a **sub-agent session**. You are not talking to the user. "
            "Your final output is a structured report consumed by your parent agent."
        )
    return ""


def build_primary_preamble() -> str:
    """Render session-context framing for primary (user-facing) sessions."""

    return (
        "# Session Context\n\n"
        "This is a **primary session**. You are talking directly to the user."
    )


def build_work_goal_instruction(work_goal: str | None) -> str:
    if work_goal is None:
        return ""
    if work_goal == "" or work_goal != work_goal.strip():
        raise ValueError("work goal must be normalized before prompt rendering")
    escaped = html.escape(work_goal, quote=False)
    return (
        "# Goal of Your Work\n\n"
        f"<work-goal>\n{escaped}\n</work-goal>\n\n"
        "This is the overarching goal of the work item you are contributing to. "
        "Your specific task may be narrower, but keep this broader goal in mind."
    )


__all__ = [
    "build_goal_instruction",
    "build_primary_preamble",
    "build_report_instruction",
    "build_spawn_preamble",
    "build_work_goal_instruction",
]
