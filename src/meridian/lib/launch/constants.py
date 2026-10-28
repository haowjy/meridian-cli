"""Shared launch constants for runners, adapters, and transport projections."""

from __future__ import annotations

from typing import Final

DRY_RUN_REPORT_PATH: Final[str] = "<spawn-report-path>"

DEFAULT_INFRA_EXIT_CODE: Final[int] = 2
POST_EXIT_PIPE_DRAIN_TIMEOUT_SECONDS: Final[float] = 1.0
REPORT_WATCHDOG_POLL_SECONDS: Final[float] = 1.0
REPORT_WATCHDOG_GRACE_SECONDS: Final[float] = 60.0
SUBPROCESS_REPORT_WATCHDOG_POLL_SECONDS: Final[float] = 5.0
CURSOR_INACTIVITY_TIMEOUT_SECONDS: Final[float] = 300.0

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
        # Pi permission policy is compiled afresh for each launch.
        "_MERIDIAN_PI_PERMISSION_POLICY",
        # Each Pi launch derives its own delivery-parent scope from its caller.
        "_MERIDIAN_PI_OWED_PARENT_IDS",
    }
)

BASE_COMMAND_CLAUDE_SUBPROCESS: Final[tuple[str, ...]] = (
    "claude",
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
)
BASE_COMMAND_CLAUDE_STREAMING: Final[tuple[str, ...]] = (
    "claude",
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
)
BASE_COMMAND_CODEX_SUBPROCESS: Final[tuple[str, ...]] = ("codex", "exec", "--json")
BASE_COMMAND_CODEX_STREAMING: Final[tuple[str, ...]] = ("codex", "app-server")
BASE_COMMAND_CURSOR_SUBPROCESS: Final[tuple[str, ...]] = (
    "cursor",
    "agent",
    "--print",
    "--output-format",
    "stream-json",
    "--trust",
)
BASE_COMMAND_OPENCODE_SUBPROCESS: Final[tuple[str, ...]] = ("opencode", "run")
BASE_COMMAND_OPENCODE_STREAMING: Final[tuple[str, ...]] = ("opencode", "serve")
BASE_COMMAND_PI_SUBPROCESS: Final[tuple[str, ...]] = ("pi", "--mode", "rpc")
BASE_COMMAND_PI_STREAMING: Final[tuple[str, ...]] = ("pi", "--mode", "rpc")

PRIMARY_BASE_COMMAND_CLAUDE: Final[tuple[str, ...]] = ("claude",)
PRIMARY_BASE_COMMAND_CODEX: Final[tuple[str, ...]] = ("codex",)
PRIMARY_BASE_COMMAND_OPENCODE: Final[tuple[str, ...]] = ("opencode",)
PRIMARY_BASE_COMMAND_PI: Final[tuple[str, ...]] = ("pi",)

__all__ = [
    "BASE_COMMAND_CLAUDE_STREAMING",
    "BASE_COMMAND_CLAUDE_SUBPROCESS",
    "BASE_COMMAND_CODEX_STREAMING",
    "BASE_COMMAND_CODEX_SUBPROCESS",
    "BASE_COMMAND_CURSOR_SUBPROCESS",
    "BASE_COMMAND_OPENCODE_STREAMING",
    "BASE_COMMAND_OPENCODE_SUBPROCESS",
    "BASE_COMMAND_PI_STREAMING",
    "BASE_COMMAND_PI_SUBPROCESS",
    "BLOCKED_CHILD_ENV_VARS",
    "CURSOR_INACTIVITY_TIMEOUT_SECONDS",
    "DEFAULT_INFRA_EXIT_CODE",
    "DRY_RUN_REPORT_PATH",
    "POST_EXIT_PIPE_DRAIN_TIMEOUT_SECONDS",
    "PRIMARY_BASE_COMMAND_CLAUDE",
    "PRIMARY_BASE_COMMAND_CODEX",
    "PRIMARY_BASE_COMMAND_OPENCODE",
    "PRIMARY_BASE_COMMAND_PI",
    "REPORT_WATCHDOG_GRACE_SECONDS",
    "REPORT_WATCHDOG_POLL_SECONDS",
    "SUBPROCESS_REPORT_WATCHDOG_POLL_SECONDS",
]
