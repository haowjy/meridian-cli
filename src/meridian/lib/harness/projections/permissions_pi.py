"""Pi's compile of the permission policy: nothing reaches Pi, and the report says so."""

from __future__ import annotations

from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import (
    EnforcementReport,
    ReportLine,
    platform_element,
    rule_element,
)


def compile_pi_permissions(spec: ResolvedLaunchSpec) -> tuple[None, EnforcementReport]:
    """Compile ``spec.permission_policy`` for Pi: no native projection, all lines unsupported."""

    policy = spec.permission_policy
    lines = [
        ReportLine(rule_element(rule), "unsupported", "Pi is given no tool rules")
        for rule in policy.rules
    ]
    lines.extend(
        ReportLine(platform_element(platform), "unsupported", "not projected for Pi")
        for platform in policy.platform_rules
    )
    if policy.sandbox != "default":
        lines.append(ReportLine(f"sandbox: {policy.sandbox}", "unsupported", "Pi has no sandbox"))
    if policy.approval != "default":
        lines.append(
            ReportLine(f"approval: {policy.approval}", "unsupported", "Pi has no approval prompts")
        )
    return None, tuple(lines)


__all__ = ["compile_pi_permissions"]
