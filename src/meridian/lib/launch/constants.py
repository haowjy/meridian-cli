"""Shared launch constants for runners, adapters, and transport projections."""

from __future__ import annotations

from typing import Final

OUTPUT_FILENAME: Final[str] = "output.jsonl"
HISTORY_FILENAME: Final[str] = "history.jsonl"
PRIMARY_META_FILENAME: Final[str] = "primary_meta.json"
STDERR_FILENAME: Final[str] = "stderr.log"
TOKENS_FILENAME: Final[str] = "tokens.json"
REPORT_FILENAME: Final[str] = "report.md"

DEFAULT_INFRA_EXIT_CODE: Final[int] = 2
POST_EXIT_PIPE_DRAIN_TIMEOUT_SECONDS: Final[float] = 1.0
REPORT_WATCHDOG_POLL_SECONDS: Final[float] = 1.0
REPORT_WATCHDOG_GRACE_SECONDS: Final[float] = 60.0
SUBPROCESS_REPORT_WATCHDOG_POLL_SECONDS: Final[float] = 5.0

BLOCKED_CHILD_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        # Child work scope must come from runtime overrides only.
        "MERIDIAN_ACTIVE_WORK_ID",
        "MERIDIAN_ACTIVE_WORK_DIR",
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
BASE_COMMAND_OPENCODE_SUBPROCESS: Final[tuple[str, ...]] = ("opencode", "run")
BASE_COMMAND_OPENCODE_STREAMING: Final[tuple[str, ...]] = ("opencode", "serve")
BASE_COMMAND_PI_SUBPROCESS: Final[tuple[str, ...]] = ("meridian-pi", "-p", "--mode", "json")
BASE_COMMAND_PI_STREAMING: Final[tuple[str, ...]] = ("meridian-pi", "--mode", "rpc")

PRIMARY_BASE_COMMAND_CLAUDE: Final[tuple[str, ...]] = ("claude",)
PRIMARY_BASE_COMMAND_CODEX: Final[tuple[str, ...]] = ("codex",)
PRIMARY_BASE_COMMAND_OPENCODE: Final[tuple[str, ...]] = ("opencode",)
PRIMARY_BASE_COMMAND_PI: Final[tuple[str, ...]] = ("meridian-pi",)

__all__ = [
    "BASE_COMMAND_CLAUDE_STREAMING",
    "BASE_COMMAND_CLAUDE_SUBPROCESS",
    "BASE_COMMAND_CODEX_STREAMING",
    "BASE_COMMAND_CODEX_SUBPROCESS",
    "BASE_COMMAND_OPENCODE_STREAMING",
    "BASE_COMMAND_OPENCODE_SUBPROCESS",
    "BASE_COMMAND_PI_STREAMING",
    "BASE_COMMAND_PI_SUBPROCESS",
    "BLOCKED_CHILD_ENV_VARS",
    "DEFAULT_INFRA_EXIT_CODE",
    "HISTORY_FILENAME",
    "OUTPUT_FILENAME",
    "POST_EXIT_PIPE_DRAIN_TIMEOUT_SECONDS",
    "PRIMARY_BASE_COMMAND_CLAUDE",
    "PRIMARY_BASE_COMMAND_CODEX",
    "PRIMARY_BASE_COMMAND_OPENCODE",
    "PRIMARY_BASE_COMMAND_PI",
    "PRIMARY_META_FILENAME",
    "REPORT_FILENAME",
    "REPORT_WATCHDOG_GRACE_SECONDS",
    "REPORT_WATCHDOG_POLL_SECONDS",
    "STDERR_FILENAME",
    "SUBPROCESS_REPORT_WATCHDOG_POLL_SECONDS",
    "TOKENS_FILENAME",
]
