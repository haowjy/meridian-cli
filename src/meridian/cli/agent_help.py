"""Agent-mode help rendering for CLI command groups."""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from cyclopts import App, Group  # noqa: TC002
from cyclopts.help.formatters.plain import PlainFormatter

from meridian.cli.help_content import GROUPS, render_group_help
from meridian.cli.help_tiers import ADVANCED_PARAMS, advanced_params_visibility

if TYPE_CHECKING:
    from cyclopts.help import HelpEntry, HelpPanel
    from rich.console import Console

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

_ADVANCED_PARAM_GROUP_NAME = ADVANCED_PARAMS.name


class AlignedPlainFormatter(PlainFormatter):
    """Plain help formatter that keeps wrapped description lines aligned."""

    def _wrap_entry(self, left: str, desc: str, console: Console) -> str:
        left_part = f"{self.indent}{left}: "
        if not desc:
            return f"{self.indent}{left}"
        desc = " ".join(desc.split())
        width = console.width
        continuation_indent = " " * len(left_part)
        if len(left_part) >= width - 20:
            block_indent = self.indent + "  "
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


def curated_group_help_target(
    argv: Sequence[str],
    *,
    root_app: App,
    registered_groups: set[str],
) -> str | None:
    """Return the group name when argv requests curated group-level help."""

    if not any(token in {"--help", "-h"} for token in argv):
        return None

    tokens = _help_tokens(argv)
    if not tokens:
        return None

    group_name = tokens[0]
    if group_name not in GROUPS or group_name not in registered_groups:
        return None

    _, apps, _ = root_app.parse_commands(tokens)
    if len(apps) != 2:
        return None

    return group_name


def _help_tokens(argv: Sequence[str]) -> list[str]:
    skip = {"--help", "-h", "--advanced", "--human", "--agent"}
    return [arg for arg in argv if arg not in skip]


def _visible_subcommands(
    group_name: str,
    *,
    agent_mode: bool,
    advanced: bool,
) -> tuple[str, ...] | None:
    if not agent_mode:
        return None

    group = GROUPS[group_name]
    if advanced:
        return group.agent_subcommands
    if group.agent_core_subcommands is not None:
        return group.agent_core_subcommands
    return group.agent_subcommands


def _command_entry_name(entry: HelpEntry) -> str:
    if entry.positive_names:
        return entry.positive_names[0]
    if entry.positive_shorts:
        return entry.positive_shorts[0]
    return ""


def _assemble_help_panels(
    root_app: App,
    tokens: list[str],
    help_format: str,
    *,
    agent_mode: bool,
    advanced: bool,
) -> list[tuple[Group | None, HelpPanel]]:
    with advanced_params_visibility(agent_mode=agent_mode, advanced=advanced):
        return root_app._assemble_help_panels(tokens, help_format)  # pyright: ignore[reportPrivateUsage]


def _filter_help_panels(
    panels: list[tuple[Group | None, HelpPanel]],
    *,
    group_name: str,
    agent_mode: bool,
    advanced: bool,
) -> list[tuple[Group | None, HelpPanel]]:
    visible = _visible_subcommands(group_name, agent_mode=agent_mode, advanced=advanced)
    filtered: list[tuple[Group | None, HelpPanel]] = []

    for group, panel in panels:
        if (
            group is not None
            and group.name == _ADVANCED_PARAM_GROUP_NAME
            and agent_mode
            and not advanced
        ):
            continue

        if panel.format == "command" and visible is not None:
            index_map = {name: index for index, name in enumerate(visible)}
            entries = [entry for entry in panel.entries if _command_entry_name(entry) in index_map]
            entries.sort(key=lambda entry: index_map[_command_entry_name(entry)])
            panel = panel.copy(entries=entries)

        filtered.append((group, panel))

    return filtered


def print_curated_group_help(
    root_app: App,
    argv: Sequence[str],
    group_name: str,
    *,
    agent_mode: bool,
    advanced: bool = False,
    console: Console | None = None,
) -> None:
    """Render curated group help from metadata without mutating parser state."""

    from cyclopts.help import InlineText, format_usage

    tokens = _help_tokens(argv)
    command_chain, apps, _ = root_app.parse_commands(tokens)
    executing_app = apps[-1]

    if console is None:
        from rich.console import Console as RichConsole

        console = RichConsole(file=sys.stdout, force_terminal=False)

    help_format = executing_app.help_format or "plaintext"
    formatter = _ALIGNED_PLAIN_FORMATTER

    if executing_app.usage is None:
        usage = format_usage(root_app, command_chain)
    elif executing_app.usage:
        usage = executing_app.usage + "\n"
    else:
        usage = None

    description = InlineText.from_format(
        render_group_help(group_name, agent_mode=agent_mode, advanced=advanced),
        format=help_format,
    )
    panels = _filter_help_panels(
        _assemble_help_panels(
            root_app,
            tokens,
            help_format,
            agent_mode=agent_mode,
            advanced=advanced,
        ),
        group_name=group_name,
        agent_mode=agent_mode,
        advanced=advanced,
    )

    if help_prologue := executing_app.help_prologue:
        prologue = InlineText.from_format(help_prologue, format=help_format)
        console.print(prologue)
        console.print()

    formatter.render_usage(console, console.options, usage)
    formatter.render_description(console, console.options, description)

    for group, panel in panels:
        panel_formatter = cast("Any", group.help_formatter if group else None)
        if panel_formatter is None:
            panel_formatter = formatter
        panel_formatter(console, console.options, panel)


__all__ = [
    "AGENT_CORE_SUBCOMMANDS",
    "AGENT_HELP_SUPPLEMENTS",
    "AGENT_VISIBLE_SUBCOMMANDS",
    "AlignedPlainFormatter",
    "curated_group_help_target",
    "print_curated_group_help",
]
