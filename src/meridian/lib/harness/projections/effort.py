"""Harness-native projection of the semantic effort level.

``none`` means reasoning off. Codex and Pi spell it natively (Codex's projector passes the
level through as ``model_reasoning_effort``). Claude Code and OpenCode have no off level, so
their projections pass nothing for it and the report says so.
"""

from __future__ import annotations

from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import EnforcementReport, ReportLine

# Pi's ``--thinking`` spelling for each effort level (pi 1.4 lists off, minimal, low,
# medium, high, xhigh, max): only ``none`` is spelled differently.
_PI_THINKING_BY_EFFORT: dict[str, str] = {
    "none": "off",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def project_claude_effort_args(effort: str | None) -> tuple[str, ...]:
    """Return Claude's ``--effort`` flag; ``none`` has no Claude spelling."""

    if not effort or effort == "none":
        return ()
    return ("--effort", effort)


def project_opencode_variant_args(spec: ResolvedLaunchSpec) -> tuple[str, ...]:
    """Return ``--variant`` for the one OpenCode path that takes it: the V1 ``run`` fallback."""

    effort = (spec.effort or "").strip()
    if not effort or effort == "none" or spec.interactive or spec.opencode_version != "v1":
        return ()
    return ("--variant", effort)


def project_pi_thinking_level(effort: str | None) -> str | None:
    """Return Pi's ``--thinking`` level for an effort, or ``None`` for an unknown one."""

    return _PI_THINKING_BY_EFFORT.get((effort or "").strip().lower())


def effort_report(spec: ResolvedLaunchSpec, *, harness: str) -> EnforcementReport:
    """Describe the effort a harness leaves unapplied."""

    effort = (spec.effort or "").strip()
    if not effort:
        return ()
    element = f"effort: {effort}"
    if harness == "Claude" and not project_claude_effort_args(effort):
        return (
            ReportLine(
                element,
                "unsupported",
                "current Claude models cannot turn thinking off; --effort omitted",
            ),
        )
    if harness == "OpenCode":
        note = f"not applied for OpenCode {'TUI' if spec.interactive else 'streaming'}"
        if project_opencode_variant_args(spec):
            note = f"{note}; the V1 run fallback still receives --variant"
        return (ReportLine(element, "unsupported", note),)
    return ()


__all__ = [
    "effort_report",
    "project_claude_effort_args",
    "project_opencode_variant_args",
    "project_pi_thinking_level",
]
