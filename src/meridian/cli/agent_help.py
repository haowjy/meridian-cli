"""Agent-mode help supplements for CLI subcommands."""

from __future__ import annotations

import logging
import textwrap
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from cyclopts import App
from cyclopts.help.formatters.plain import PlainFormatter

from meridian.cli.help_content import GROUPS, render_group_help
from meridian.cli.help_tiers import ADVANCED_PARAMS

if TYPE_CHECKING:
    from cyclopts.help import HelpEntry
    from rich.console import Console

logger = logging.getLogger(__name__)


class AlignedPlainFormatter(PlainFormatter):
    """Plain help formatter that keeps wrapped description lines aligned."""

    def _wrap_entry(self, left: str, desc: str, console: Console) -> str:
        left_part = f"{self.indent}{left}: "
        if not desc:
            return f"{self.indent}{left}"
        width = console.width
        continuation_indent = " " * len(left_part)
        if len(left_part) >= width - 20:
            block_indent = self.indent
            return left_part.rstrip() + "\n" + textwrap.fill(
                desc,
                width=width,
                initial_indent=block_indent,
                subsequent_indent=block_indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
        return textwrap.fill(
            desc,
            width=width,
            initial_indent=left_part,
            subsequent_indent=continuation_indent,
            break_long_words=False,
            break_on_hyphens=False,
        )

    def _format_parameter_entry(
        self,
        options: tuple[str, ...],
        desc: str,
        console: Console,
        entry: HelpEntry,
    ) -> None:
        if not options:
            return

        desc_parts: list[str] = []
        if desc:
            desc_parts.append(desc)
        if entry.choices:
            desc_parts.append(f"[choices: {', '.join(entry.choices)}]")
        if entry.env_var:
            desc_parts.append(f"[env var: {', '.join(entry.env_var)}]")
        if entry.default is not None:
            desc_parts.append(f"[default: {entry.default}]")
        if entry.required:
            desc_parts.append("[required]")

        options_str = ", ".join(options)
        left = options_str if options_str else ""
        wrapped = self._wrap_entry(left, " ".join(desc_parts), console)
        console.print(wrapped, highlight=False, markup=False)

    def _format_command_entry(
        self,
        names: tuple[str, ...],
        shorts: tuple[str, ...],
        desc: str,
        console: Console,
    ) -> None:
        if names:
            for index, name in enumerate(names):
                if index == 0:
                    parts = [name]
                    if shorts:
                        parts.append(", " + " ".join(shorts))
                    entry_name = "".join(parts)
                    console.print(
                        self._wrap_entry(entry_name, desc, console),
                        highlight=False,
                        markup=False,
                    )
                else:
                    console.print(
                        textwrap.indent(name, self.indent),
                        highlight=False,
                        markup=False,
                    )
        elif shorts:
            shorts_str = " ".join(shorts)
            console.print(
                self._wrap_entry(shorts_str, desc, console),
                highlight=False,
                markup=False,
            )


_ALIGNED_PLAIN_FORMATTER = AlignedPlainFormatter()


@dataclass(frozen=True)
class _SubcommandVisibility:
    show: bool
    sort_key: Any


@dataclass(frozen=True)
class _GroupBaseline:
    help: str | None
    help_epilogue: str | None
    help_formatter: object | None
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

# Only spawn tiers its parameter group today. Keep this explicit until another
# group owns advanced-only parameters.
_GROUPS_WITH_ADVANCED_PARAMS = frozenset({"spawn"})


def _set_advanced_params_visibility(group_name: str, *, agent_mode: bool, advanced: bool) -> None:
    if group_name in _GROUPS_WITH_ADVANCED_PARAMS:
        setattr(ADVANCED_PARAMS, "_show", (not agent_mode) or advanced)  # noqa: B010


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
        help_formatter=group_app.help_formatter,
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
    group_app.help_formatter = baseline.help_formatter


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
    _set_advanced_params_visibility(group_name, agent_mode=agent_mode, advanced=advanced)
    group_app.help_formatter = _ALIGNED_PLAIN_FORMATTER
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
    _set_advanced_params_visibility(group_name, agent_mode=False, advanced=advanced)
    group_app.help = render_group_help(group_name, agent_mode=False)
    group_app.help_epilogue = ""


__all__ = [
    "AGENT_CORE_SUBCOMMANDS",
    "AGENT_HELP_SUPPLEMENTS",
    "AGENT_VISIBLE_SUBCOMMANDS",
    "apply_agent_help",
]
