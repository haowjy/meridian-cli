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
    Option("-m, --model TEXT", "Model id or alias for primary harness.", False),
    Option("--harness TEXT", "Force harness id (claude, codex, cursor, opencode, or pi).", False),
    Option("--format TEXT", "Set output format: text or json.", False),
    Option("--json", "Emit command output as JSON.", False),
    Option("-C, --directory PATH", "Resolve project root from this path instead of CWD.", True),
    Option("--config TEXT", "Path to a user config TOML overlay.", False),
    Option("-h, --help", "Show this message and exit.", False),
    Option("-v, --version", "Show the application version.", False),
)

GROUP_DESCRIPTIONS: dict[str, str] = {name: group.summary for name, group in GROUPS.items()}

AGENT_DESCRIPTION_OVERRIDES: dict[str, str] = {
    "spawn": (
        "Hand a task to a subagent. Runs in the background; you keep working\n"
        "and collect the result later. (`fork`, `wait`, `report`)"
    ),
    "session": (
        "Read the full transcript of any spawn — what it did, what it found —\n"
        "so you build on it instead of redoing it."
    ),
    "work": (
        "Tie related spawns to one goal with a shared folder and history, so\n"
        "a multi-step effort holds together across handoffs."
    ),
    "context": (
        "Shows the folders (as env vars) where shared files and project\n"
        "knowledge live, so you read and write them in the right place."
    ),
    "mars": (
        "List the models, agents, and skills available, so you pick real ones\n"
        "when you delegate. (`meridian mars models list`)"
    ),
    "ext": "Run project-specific extension commands.",
}

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
    "context",
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
    "mktemp",
    "doctor",
    "bootstrap",
    "ext",
)

AGENT_ORIENTATION: tuple[str, ...] = (
    "Meridian lets you hand work to other agents instead of doing everything\n"
    "yourself. Launch a subagent on a slice of the task — a bulk read, a parallel\n"
    "fix, a stronger or cheaper model — and keep your own context for the parts\n"
    "that need your judgment. What each subagent does is recorded, so you can\n"
    "check a running spawn, read its result when it finishes, or pick up what an\n"
    "earlier agent already worked out.",
)

HUMAN_TAGLINE = "Multi-agent orchestration across Claude, Codex, and OpenCode."

QUICK_START_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("meridian spawn -m MODEL --prompt-file /tmp/task.md --bg", ""),
    ("meridian spawn wait", ""),
    ("meridian mars models list", ""),
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
    continuation_indent = " " * (2 + width + 2)
    rendered: list[str] = []
    for left, right in rows:
        if right:
            right_lines = right.split("\n")
            first = f"  {left.ljust(width)}  {right_lines[0]}"
            rest = [f"{continuation_indent}{line}" for line in right_lines[1:]]
            rendered.append("\n".join([first, *rest]))
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

    if not agent_mode:
        sections.append(
            "Primary launch/resume:\n"
            f"{_render_table(list(LAUNCH_EXAMPLES))}\n"
            f"{LAUNCH_REF_NOTES}"
        )

    sections.append(f"Commands:\n{_render_table(_command_rows(agent_mode=agent_mode))}")

    if agent_mode:
        sections.append(f"Quick start:\n{_render_table(list(QUICK_START_EXAMPLES))}")
        sections.append(
            "For details on any command:\n"
            "  `meridian spawn -h`\n"
            "  `meridian work -h`\n"
            "  `meridian session -h`\n"
            "  `meridian context -h`"
        )
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
