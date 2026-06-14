"""Agent-mode help supplements for CLI subcommands."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, cast

from cyclopts import App

from meridian.cli.help_content import GROUPS, render_group_help

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SubcommandVisibility:
    show: bool
    sort_key: Any


@dataclass(frozen=True)
class _GroupBaseline:
    help: str | None
    help_epilogue: str | None
    subcommands: dict[int, _SubcommandVisibility]


_BASELINE: dict[str, _GroupBaseline] = {}

AGENT_VISIBLE_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    name: group.agent_subcommands
    for name, group in GROUPS.items()
    if group.agent_subcommands is not None
}

AGENT_CORE_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    name: group.agent_core_subcommands
    for name, group in GROUPS.items()
    if group.agent_core_subcommands is not None
}

AGENT_HELP_SUPPLEMENTS: dict[str, str] = {
    name: group.agent_notes for name, group in GROUPS.items() if group.agent_notes is not None
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
        help=group_app.help,
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

    group_app.help = baseline.help
    group_app.help_epilogue = baseline.help_epilogue


def _apply_subcommand_visibility(
    group_app: App,
    group_name: str,
    visible: tuple[str, ...] | None = None,
) -> None:
    """Hide and order subcommands for agent-mode group help.

    Reaches into cyclopts' private ``_commands`` dict deliberately — help
    curation has no public API. Re-check this on cyclopts upgrades.
    """

    if visible is None:
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


def apply_agent_help(
    group_app: App,
    group_name: str,
    *,
    agent_mode: bool,
    advanced: bool = False,
) -> None:
    """Apply or restore mode-specific help curation for one command group."""

    if group_name not in GROUPS:
        return

    _snapshot_baseline(group_app, group_name)
    if agent_mode:
        visible = (
            AGENT_VISIBLE_SUBCOMMANDS.get(group_name)
            if advanced
            else AGENT_CORE_SUBCOMMANDS.get(group_name, AGENT_VISIBLE_SUBCOMMANDS.get(group_name))
        )
        if visible is not None:
            _apply_subcommand_visibility(group_app, group_name, visible)
        group_app.help = render_group_help(group_name, agent_mode=True, advanced=advanced)
        group_app.help_epilogue = ""
        return

    _restore_baseline(group_app, group_name)
    group_app.help = render_group_help(group_name, agent_mode=False)
    group_app.help_epilogue = ""


__all__ = [
    "AGENT_CORE_SUBCOMMANDS",
    "AGENT_HELP_SUPPLEMENTS",
    "AGENT_VISIBLE_SUBCOMMANDS",
    "apply_agent_help",
]
