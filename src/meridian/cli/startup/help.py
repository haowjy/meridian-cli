"""Startup-cheap root help rendering for Meridian CLI."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from meridian import __version__
from meridian.cli.help_content import GROUPS
from meridian.cli.startup.catalog import COMMAND_CATALOG


@dataclass(frozen=True)
class Option:
    """Root help option row."""

    flags: str
    help: str
    agent: bool


OPTIONS: tuple[Option, ...] = (
    Option("-m, --model TEXT", "Model id or alias for primary harness.", True),
    Option("--harness TEXT", "Force harness id (claude, codex, cursor, opencode, or pi).", True),
    Option("--format TEXT", "Set output format: text or json.", True),
    Option("--json", "Emit command output as JSON.", True),
    Option("-C, --directory PATH", "Resolve project root from this path instead of CWD.", True),
    Option("--config TEXT", "Path to a user config TOML overlay.", True),
    Option("-h, --help", "Show this message and exit.", False),
    Option("-v, --version", "Show the application version.", False),
)

GROUP_DESCRIPTIONS: dict[str, str] = {name: group.summary for name, group in GROUPS.items()}

AGENT_DESCRIPTION_OVERRIDES: dict[str, str] = {}

LAUNCH_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("meridian -m MODEL", "Launch the primary harness"),
    ("meridian --continue c123", "Resume from ref"),
    ("meridian --fork p123", "Fork from ref"),
    ("meridian --fork-fresh p123 -m MODEL", "Fork and switch identity"),
    ("meridian --from p123", "Fresh session with prior context"),
    ("meridian --task-dir PATH", "Launch against a different edit directory"),
)

LAUNCH_REF_NOTES = (
    "  continue/fork refs: chat id (c123), spawn id (p123), "
    "or raw harness session id\n"
    "  --from refs: chat id (c123) or spawn id (p123)\n"
    "  --fork preserves agent/model/skills identity. --fork-fresh allows\n"
    "  identity changes and may reduce prompt-cache locality. --from starts\n"
    "  fresh with prior context as reference material only."
)

AGENT_ROOT_COMMANDS: tuple[str, ...] = (
    "spawn",
    "session",
    "work",
    "config",
    "context",
    "doctor",
    "mars",
    "ext",
)

_HUMAN_COMMAND_ORDER = (
    "spawn",
    "session",
    "work",
    "hooks",
    "sync",
    "models",
    "streaming",
    "test",
    "config",
    "workspace",
    "kg",
    "mermaid",
    "telemetry",
    "completion",
    "serve",
    "mars",
    "init",
    "chat",
    "doctor",
    "bootstrap",
    "ext",
)

AGENT_ORIENTATION: tuple[str, ...] = (
    "Multi-agent orchestration CLI. Meridian is a coordination layer — it launches\n"
    "subagents through harness adapters and persists state to disk. It is not a\n"
    "runtime, database, or workflow engine.",
    "State on disk is the source of truth. Inspect via CLI commands; treat state\n"
    "files under the state root as implementation detail — do not hand-edit.\n"
    "Operations are idempotent: re-running after interruption converges to correct\n"
    "state.",
    "For automation, use --format json and parse fields from JSON responses.\n"
    "Avoid scraping prose from text output.",
)

HUMAN_TAGLINE = "Multi-agent orchestration across Claude, Codex, and OpenCode."

QUICK_START_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("meridian spawn -m MODEL --prompt-file /tmp/task.md --bg", "Launch a subagent"),
    ("meridian spawn wait", "Wait for all pending spawns"),
    ("meridian mars models list", "See model catalog inventory"),
)


def _detect_agent_mode(*, force_agent: bool = False, force_human: bool = False) -> bool:
    """Detect agent mode from env and terminal state."""

    if force_agent:
        return True
    if force_human:
        return False
    from meridian.lib.core.depth import is_managed_meridian_session

    return is_managed_meridian_session() and not (sys.stdin.isatty() and sys.stdout.isatty())


def _render_table(rows: tuple[tuple[str, str], ...] | list[tuple[str, str]]) -> str:
    width = max((len(left) for left, _right in rows), default=0)
    rendered: list[str] = []
    for left, right in rows:
        if right:
            rendered.append(f"  {left.ljust(width)}  {right}")
        else:
            rendered.append(f"  {left}")
    return "\n".join(rendered)


def _human_command_names() -> list[str]:
    names = COMMAND_CATALOG.top_level_names()
    ordered = [name for name in _HUMAN_COMMAND_ORDER if name in names]
    ordered.extend(sorted(names - set(ordered)))
    return ordered


def _command_rows(*, agent_mode: bool) -> list[tuple[str, str]]:
    names = list(AGENT_ROOT_COMMANDS) if agent_mode else _human_command_names()
    rows: list[tuple[str, str]] = []
    for name in names:
        description = GROUP_DESCRIPTIONS[name]
        if agent_mode:
            description = AGENT_DESCRIPTION_OVERRIDES.get(name, description)
        rows.append((name, description))
    return rows


def _option_rows(*, agent_mode: bool) -> list[tuple[str, str]]:
    return [(option.flags, option.help) for option in OPTIONS if not agent_mode or option.agent]


def detect_agent_mode(*, force_agent: bool = False, force_human: bool = False) -> bool:
    """Detect whether startup should render agent-mode help."""

    return _detect_agent_mode(force_agent=force_agent, force_human=force_human)


def render_root_help(*, agent_mode: bool) -> str:
    """Render root help text for the given mode."""

    sections = ["Usage: meridian [OPTIONS] [COMMAND]"]
    if agent_mode:
        sections.append("\n\n".join(AGENT_ORIENTATION))
    else:
        sections.append(HUMAN_TAGLINE)

    sections.append(f"Options:\n{_render_table(_option_rows(agent_mode=agent_mode))}")
    sections.append(
        "Primary launch/resume:\n"
        f"{_render_table(list(LAUNCH_EXAMPLES))}\n"
        f"{LAUNCH_REF_NOTES}"
    )

    if agent_mode:
        sections.append(f"Quick start:\n{_render_table(list(QUICK_START_EXAMPLES))}")

    sections.append(f"Commands:\n{_render_table(_command_rows(agent_mode=agent_mode))}")

    if agent_mode:
        sections.append("Run 'meridian spawn -h' for full spawn usage.")
    else:
        sections.append(
            "Global harness selection: --harness (or prefix with claude/codex/cursor/opencode)\n\n"
            "Bundled package manager: meridian mars ARGS...\n\n"
            "Run \"meridian spawn -h\" for subagent usage.\n\n"
            f"Version: {__version__}"
        )

    return "\n\n".join(sections) + "\n"


__all__ = [
    "AGENT_DESCRIPTION_OVERRIDES",
    "AGENT_ROOT_COMMANDS",
    "GROUP_DESCRIPTIONS",
    "LAUNCH_EXAMPLES",
    "OPTIONS",
    "Option",
    "_detect_agent_mode",
    "detect_agent_mode",
    "render_root_help",
]
