from __future__ import annotations

from typing import Final

DRY_RUN_REPORT_PATH: Final[str] = "<spawn-report-path>"

DEFAULT_INFRA_EXIT_CODE: Final[int] = 2
POST_EXIT_PIPE_DRAIN_TIMEOUT_SECONDS: Final[float] = 1.0
REPORT_WATCHDOG_POLL_SECONDS: Final[float] = 1.0
REPORT_WATCHDOG_GRACE_SECONDS: Final[float] = 60.0
SUBPROCESS_REPORT_WATCHDOG_POLL_SECONDS: Final[float] = 5.0
# How often a streaming runner touches its lifecycle log so a crashed launch has a recent mtime.
RUNNER_HEARTBEAT_SECONDS: Final[float] = 5.0

BLOCKED_CHILD_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        # Child work scope must come from runtime overrides only.
        "MERIDIAN_ACTIVE_WORK_ID",
        "MERIDIAN_ACTIVE_WORK_DIR",
        # Launch identity is resolved per child and must never leak from its parent.
        "MERIDIAN_SESSION_AGENT",
        # Never inherited from the parent env; the bind seam sets it on a child only
        # when the launcher's own runtime came from an override (launch/context.py).
        "_MERIDIAN_RUNTIME_DIR",
        # Caller confinement is observed and set by this launcher, never inherited.
        "_MERIDIAN_CALLER_BOUNDARY",
        # Names one launch's starting-prompt file; a launch that delivers no prompt
        # through the mod must not inherit its parent's.
        "_MERIDIAN_CLAUDE_FIRST_TURN_PATH",
        # The scoped-tool policy is compiled afresh for each launch.
        "_MERIDIAN_TOOL_POLICY",
        "_MERIDIAN_TOOL_EVALUATOR",
        # The name Pi bundles built before the rename read (LEGACY_PI_POLICY_ENV).
        "_MERIDIAN_PI_PERMISSION_POLICY",
        # Each Pi launch derives its own delivery-parent scope from its caller.
        "_MERIDIAN_PI_OWED_PARENT_IDS",
        # Set by the Pi managed-bash task that ran this launcher; a harness child has its own.
        "_MERIDIAN_PI_BASH_ID",
        "_MERIDIAN_PI_BASH_HOLD_DIR",
        # Both name one Pi launch's endpoint and launch prompt; a nested launch derives its own.
        "_MERIDIAN_PI_LAUNCH_PROMPT",
        "_MERIDIAN_PI_CONTROL_SOCKET",
    }
)

__all__ = [
    "BLOCKED_CHILD_ENV_VARS",
    "DEFAULT_INFRA_EXIT_CODE",
    "DRY_RUN_REPORT_PATH",
    "POST_EXIT_PIPE_DRAIN_TIMEOUT_SECONDS",
    "REPORT_WATCHDOG_GRACE_SECONDS",
    "REPORT_WATCHDOG_POLL_SECONDS",
    "RUNNER_HEARTBEAT_SECONDS",
    "SUBPROCESS_REPORT_WATCHDOG_POLL_SECONDS",
]
