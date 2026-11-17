"""Harness-native spelling of the effort level Mars resolved.

Mars moves effort to a level the model and harness take, so nothing here clamps. ``none``
is reasoning off; Codex's projector passes the level through as ``model_reasoning_effort``
and Pi spells ``none`` as ``off``.
"""

from __future__ import annotations

from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import EnforcementReport, ReportLine


def project_opencode_variant_args(spec: ResolvedLaunchSpec) -> tuple[str, ...]:
    """Return ``--variant`` for the one OpenCode path that takes it: the V1 ``run`` fallback."""

    effort = (spec.effort or "").strip()
    # OpenCode documents no `none` variant, so reasoning off passes nothing.
    if not effort or effort == "none" or spec.interactive or spec.opencode_version != "v1":
        return ()
    return ("--variant", effort)


def project_pi_thinking_level(effort: str | None) -> str | None:
    """Return Pi's ``--thinking`` value: Mars's level, with ``none`` spelled ``off``."""

    level = (effort or "").strip().lower()
    return ("off" if level == "none" else level) or None


def opencode_effort_report(spec: ResolvedLaunchSpec) -> EnforcementReport:

    effort = (spec.effort or "").strip()
    if not effort:
        return ()
    note = f"not applied for OpenCode {'TUI' if spec.interactive else 'streaming'}"
    if project_opencode_variant_args(spec):
        note = f"{note}; the V1 run fallback still receives --variant"
    return (ReportLine(f"effort: {effort}", "unsupported", note),)


__all__ = [
    "opencode_effort_report",
    "project_opencode_variant_args",
    "project_pi_thinking_level",
]
