"""Agent-mode help supplements for CLI subcommands."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, cast

from cyclopts import App

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SubcommandVisibility:
    show: bool
    sort_key: Any


@dataclass(frozen=True)
class _GroupBaseline:
    help_epilogue: str | None
    subcommands: dict[int, _SubcommandVisibility]


_BASELINE: dict[str, _GroupBaseline] = {}

# Agent-visible subcommands per top-level group. Tuple order = help display order.
# Subcommands registered but absent here stay visible in human mode only.
AGENT_VISIBLE_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    # spawn: the agent's primary surface. Hide status (near-dup of show; use
    # `show --no-report`), stats (analytics), create/continue (driven by the
    # default route and --continue/--fork flags, not needed as listed subcommands).
    "spawn": (
        "show",
        "wait",
        "list",
        "children",
        "files",
        "report",
        "inject",
        "done",
        "rearm",
        "cancel",
        "cancel-all",
    ),
    "session": ("log", "search", "export"),
    "work": (
        "start",
        "current",
        "root",
        "done",
        "sessions",
        "list",
        "show",
        "switch",
    ),
    "config": ("show", "get", "set", "init"),
}

_SPAWN_SUPPLEMENT = (
    "Agent Notes:\n\n"
    "Lifecycle: queued → running → finalizing → succeeded | failed | cancelled | timed_out.\n"
    "'finalizing' is transient — treat as active when polling.\n\n"
    "Transcripts: 'meridian session log ID'.\n"
)

_SESSION_SUPPLEMENT = (
    "Agent Notes:\n\n"
    "REF forms: chat id (c123), spawn id (p123), or harness session id.\n\n"
    "Omitting REF defaults to the top-level primary session at every depth.\n"
    "Pass an explicit spawn id to inspect a specific spawn's transcript.\n\n"
    "Decision recovery: 'meridian work sessions WORK_ID --all'\n"
)

_CONFIG_SUPPLEMENT = (
    "Agent Notes:\n\n"
    "Resolution is per field:\n"
    "CLI flag > env var > profile > project > user > harness default\n\n"
    "A CLI model override (-m) also drives harness routing.\n"
)

_WORK_SUPPLEMENT = (
    "Agent Notes:\n\n"
    "Artifact placement: $MERIDIAN_ACTIVE_WORK_DIR for this item,\n"
    "$MERIDIAN_CONTEXT_KB_DIR for project-wide knowledge.\n"
)

_DOCTOR_SUPPLEMENT = (
    "Agent Notes:\n\n"
    "Run when a spawn seems stuck or status doesn't match reality.\n"
    "Spawn read paths (show, list, wait) and 'doctor' reconcile orphans.\n\n"
    "Common failure modes:\n\n"
    "  orphan_run              Runner died mid-flight. Relaunch.\n\n"
    "  orphan_finalization     Exited without finalizing. Check 'spawn show'\n"
    "                          for partial report.\n\n"
    "  Exit 127 / empty report Harness binary missing from PATH.\n\n"
    "  Exit 143 or 137         Check 'spawn show' first — if already\n"
    "                          succeeded, signal hit during cleanup.\n"
    "                          Otherwise retry.\n\n"
    "For the transcript: 'meridian session log SPAWN_ID'.\n"
)

AGENT_HELP_SUPPLEMENTS: dict[str, str] = {
    "spawn": _SPAWN_SUPPLEMENT,
    "session": _SESSION_SUPPLEMENT,
    "config": _CONFIG_SUPPLEMENT,
    "work": _WORK_SUPPLEMENT,
    "doctor": _DOCTOR_SUPPLEMENT,
}


def _iter_real_subcommands(group_app: App) -> dict[str, App]:
    # Deliberately reaches into cyclopts' private ``_commands`` dict: help
    # curation has no public API. Keep this boundary narrow and re-check it on
    # cyclopts upgrades.
    try:
        commands_obj = object.__getattribute__(group_app, "_commands")
    except AttributeError:
        logger.debug(
            "cyclopts App missing _commands; skipping agent help for %s",
            getattr(group_app, "name", group_app),
        )
        return {}

    if not isinstance(commands_obj, dict):
        logger.debug(
            "cyclopts App._commands is not a dict; skipping agent help for %s",
            getattr(group_app, "name", group_app),
        )
        return {}

    commands = cast("dict[str, object]", commands_obj)
    real_commands: dict[str, App] = {}
    for name, subcommand in commands.items():
        if isinstance(subcommand, App):
            real_commands[name] = subcommand
        else:
            logger.debug(
                "cyclopts command %s is not an App; skipping for agent help",
                name,
            )
    return real_commands


def _snapshot_baseline(group_app: App, group_name: str) -> None:
    if group_name in _BASELINE:
        return

    commands = _iter_real_subcommands(group_app)

    subcommands: dict[int, _SubcommandVisibility] = {}
    for name, subcommand in commands.items():
        if name.startswith("-"):
            continue
        sub_id = id(subcommand)
        if sub_id not in subcommands:
            subcommands[sub_id] = _SubcommandVisibility(
                show=subcommand.show,
                sort_key=subcommand.sort_key,
            )

    _BASELINE[group_name] = _GroupBaseline(
        help_epilogue=group_app.help_epilogue,
        subcommands=subcommands,
    )


def _restore_baseline(group_app: App, group_name: str) -> None:
    baseline = _BASELINE.get(group_name)
    if baseline is None:
        return

    commands = _iter_real_subcommands(group_app)

    for name, subcommand in commands.items():
        if name.startswith("-"):
            continue
        visibility = baseline.subcommands.get(id(subcommand))
        if visibility is None:
            continue
        subcommand.show = visibility.show
        subcommand.sort_key = visibility.sort_key

    group_app.help_epilogue = baseline.help_epilogue


def _apply_subcommand_visibility(group_app: App, group_name: str) -> None:
    """Hide and order subcommands for agent-mode group help.

    Reaches into cyclopts' private ``_commands`` dict deliberately — help
    curation has no public API. Re-check this on cyclopts upgrades.
    """

    visible = AGENT_VISIBLE_SUBCOMMANDS.get(group_name)
    if visible is None:
        return

    commands = _iter_real_subcommands(group_app)

    index_map = {name: index for index, name in enumerate(visible)}
    names_by_app: dict[int, list[str]] = defaultdict(list)
    app_by_id: dict[int, Any] = {}

    for name, subcommand in commands.items():
        if name.startswith("-"):
            continue
        sub_id = id(subcommand)
        names_by_app[sub_id].append(name)
        app_by_id[sub_id] = subcommand

    for sub_id, names in names_by_app.items():
        subcommand = app_by_id[sub_id]
        matching = [name for name in names if name in index_map]
        if matching:
            subcommand.show = True
            subcommand.sort_key = min(index_map[name] for name in matching)
        else:
            subcommand.show = False


def apply_agent_help(group_app: App, group_name: str, *, agent_mode: bool) -> None:
    """Apply or restore agent-mode help curation for one command group."""

    if group_name not in AGENT_HELP_SUPPLEMENTS:
        return

    _snapshot_baseline(group_app, group_name)
    if agent_mode:
        if group_name in AGENT_VISIBLE_SUBCOMMANDS:
            _apply_subcommand_visibility(group_app, group_name)
        baseline = _BASELINE.get(group_name)
        base_epilogue = baseline.help_epilogue if baseline is not None else group_app.help_epilogue
        group_app.help_epilogue = agent_help_epilogue(group_name, base_epilogue)
        return

    _restore_baseline(group_app, group_name)


def agent_help_epilogue(command_name: str, base_epilogue: str | None = None) -> str | None:
    """Return a command epilogue with the agent supplement appended, if any."""

    supplement = AGENT_HELP_SUPPLEMENTS.get(command_name)
    if supplement is None:
        return base_epilogue

    existing = base_epilogue or ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    return existing + "\n" + supplement


__all__ = [
    "AGENT_HELP_SUPPLEMENTS",
    "AGENT_VISIBLE_SUBCOMMANDS",
    "agent_help_epilogue",
    "apply_agent_help",
]
