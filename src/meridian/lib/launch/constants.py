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
        # The scoped-tool policy is compiled afresh for each launch.
        "_MERIDIAN_TOOL_POLICY",
        "_MERIDIAN_TOOL_EVALUATOR",
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
