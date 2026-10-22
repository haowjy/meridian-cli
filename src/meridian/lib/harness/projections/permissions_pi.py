"""Pi's compile of the permission policy: nothing reaches Pi, and the report says so."""

from __future__ import annotations

from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.policy import EnforcementReport, complete_report


def compile_pi_permissions(spec: ResolvedLaunchSpec) -> tuple[None, EnforcementReport]:
    """Compile ``spec.permission_policy`` for Pi: no native projection, all lines unsupported."""

    return None, complete_report(spec.permission_policy, (), harness="Pi")


__all__ = ["compile_pi_permissions"]
