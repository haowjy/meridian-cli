"""Startup-cheap root help rendering for Meridian CLI."""

from __future__ import annotations

import sys

from meridian import __version__
from meridian.cli.startup.catalog import COMMAND_CATALOG

AGENT_ROOT_HELP = """Usage: meridian COMMAND [ARGS]

Multi-agent orchestration CLI. Meridian is a coordination layer — it launches
subagents through harness adapters and persists state to disk. It is not a
runtime, database, or workflow engine.

State on disk is the source of truth. Inspect via CLI commands; treat state
files under the state root as implementation detail — do not hand-edit.
Operations are idempotent: re-running after interruption converges to correct
state.

For automation, use --format json and parse fields from JSON responses.
Avoid scraping prose from text output.

Primary launch/resume:
  meridian -m MODEL                     Launch the primary harness
  meridian --continue c123              Resume from ref
  meridian --fork p123                  Fork from ref
  meridian --fork-fresh p123 -m MODEL   Fork and switch identity
  refs: chat id (c123), spawn id (p123), or raw harness session id
  --fork preserves agent/model/skills identity. --fork-fresh allows
  identity changes and may reduce prompt-cache locality.

Quick start:
  meridian spawn -m MODEL --prompt-file /tmp/task.md --bg   Launch a subagent
  meridian spawn wait                        Wait for all pending spawns
  meridian mars models list                  See available models

Commands:
  spawn    Create and manage subagent runs
  session  Inspect transcripts and progress logs
  work     Work item dashboard and coordination
  config   Show resolved configuration and sources
  context  Show context paths for work and knowledge
  telemetry Tail, query, and inspect local telemetry segments
  doctor   Health check and orphan reconciliation
  mars     Package management and agent materialization
  ext      Extension command discovery and invocation

Run 'meridian spawn -h' for full spawn usage.
"""

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

_HUMAN_COMMAND_SUMMARIES = {
    "spawn": "Run subagents with a model and prompt file.",
    "session": "Inspect conversation and progress logs.",
    "work": "Active activity grouped by work, plus work item coordination commands.",
    "hooks": "Hook inspection and execution commands",
    "sync": "Sync operations and autosync conflict management.",
    "models": "Model catalog commands",
    "streaming": "Streaming layer commands",
    "test": "Focused test and demo commands",
    "config": "Repository-level config for defaults and resolved values.",
    "context": "Show context paths for work and knowledge.",
    "workspace": "Workspace topology commands.",
    "kg": "Knowledge graph analysis: document relationships and link health.",
    "mermaid": "Mermaid diagram validation.",
    "telemetry": "Telemetry inspection: tail, query, and status over local segments.",
    "completion": "Shell completion helpers",
    "serve": "Start FastMCP server on stdio.",
    "mars": "Forward arguments to the bundled mars CLI.",
    "init": "Initialize meridian in a project.",
    "chat": "Start interactive chat service.",
    "doctor": "Run doctor checks.",
    "bootstrap": "Bootstrap an agent runtime.",
    "ext": "Extension command discovery and invocation.",
}


def _detect_agent_mode(*, force_agent: bool = False, force_human: bool = False) -> bool:
    """Detect agent mode from env and terminal state."""

    if force_agent:
        return True
    if force_human:
        return False
    from meridian.lib.core.depth import is_nested_meridian_process

    return is_nested_meridian_process() and not (sys.stdin.isatty() and sys.stdout.isatty())


def _human_command_lines() -> list[str]:
    names = COMMAND_CATALOG.top_level_names()
    ordered = [name for name in _HUMAN_COMMAND_ORDER if name in names]
    ordered.extend(sorted(names - set(ordered)))
    width = max(len(name) for name in ordered) if ordered else 0
    return [
        f"  {name.ljust(width)}  {_HUMAN_COMMAND_SUMMARIES.get(name, name)}" for name in ordered
    ]


def _render_human_root_help() -> str:
    command_lines = "\n".join(_human_command_lines())
    return f"""Usage: meridian [ARGS] [COMMAND]

Multi-agent orchestration across Claude, Codex, and OpenCode.

Options:
  --help, -h        Show this message and exit.
  --version         Show the application version.
  --json            Emit command output as JSON.
  --format TEXT     Set output format: text or json.
  --config TEXT     Path to a user config TOML overlay.
  --harness TEXT    Force harness id (claude, codex, or opencode).
  --model, -m TEXT  Model id or alias for primary harness.

Commands:
{command_lines}

Primary launch/resume:

  meridian [-m MODEL]

  meridian --continue c123

  meridian --fork p123

  meridian --fork-fresh p123 -m MODEL

  refs: chat id (c123), spawn id (p123), or raw harness session id
  --fork preserves agent/model/skills identity. --fork-fresh allows
  identity changes and may reduce prompt-cache locality.

Global harness selection: --harness (or prefix with claude/codex/opencode)

Bundled package manager: meridian mars ARGS...

Run "meridian spawn -h" for subagent usage.

Version: {__version__}
"""


def detect_agent_mode(*, force_agent: bool = False, force_human: bool = False) -> bool:
    """Detect whether startup should render agent-mode help."""

    return _detect_agent_mode(force_agent=force_agent, force_human=force_human)


def render_root_help(*, agent_mode: bool) -> str:
    """Render root help text for the given mode."""

    if agent_mode:
        return AGENT_ROOT_HELP
    return _render_human_root_help()


__all__ = ["AGENT_ROOT_HELP", "_detect_agent_mode", "detect_agent_mode", "render_root_help"]
