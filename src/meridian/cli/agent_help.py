"""Mode-aware Cyclopts help rendering for the Meridian CLI.

This module is the single owner for help display policy: given a resolved
``RenderMode`` (via ``agent_mode`` / ``advanced`` flags) and per-command
metadata (``GROUPS``, catalog), it renders curated group help or scopes leaf
help so parent group epilogues do not leak. ``main`` should resolve mode and
dispatch here — it must not mutate shared ``App`` help state for rendering.
"""

from __future__ import annotations

import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast, get_args

from attrs import evolve
from cyclopts.help.formatters.plain import PlainFormatter

from meridian.cli.command_groups import (
    AGENT_CORE_SUBCOMMANDS,
    AGENT_HELP_SUPPLEMENTS,
    AGENT_VISIBLE_SUBCOMMANDS,
    GROUPS,
    group_app,
    group_apps_with_static_apps,
    render_group_help,
)
from meridian.cli.cyclopts_internals import assemble_help_panels
from meridian.cli.help_tiers import ADVANCED_PARAMS, advanced_params_visibility

if TYPE_CHECKING:
    from cyclopts import App, Group
    from cyclopts.help import HelpEntry, HelpPanel
    from rich.console import Console

_ADVANCED_PARAM_GROUP_NAME = ADVANCED_PARAMS.name


def _negative_names_are_meaningful(entry: HelpEntry) -> bool:
    """Return whether a negative form changes the parameter's default behavior."""

    if entry.default == "True":
        return True

    hint_args = get_args(entry.type)
    return (entry.required and entry.type is bool) or (
        entry.default is None and bool in hint_args and type(None) in hint_args
    )


def _compact_parameter_entry(entry: HelpEntry) -> HelpEntry:
    """Hide generated aliases without changing which forms Cyclopts accepts."""

    positive_names = entry.positive_names
    if any(name.startswith("--") for name in positive_names):
        positive_names = tuple(name for name in positive_names if name.startswith("-"))

    if _negative_names_are_meaningful(entry):
        negative_names = entry.negative_names
        negative_shorts = entry.negative_shorts
    else:
        negative_names = ()
        negative_shorts = ()

    return evolve(
        entry,
        positive_names=positive_names,
        negative_names=negative_names,
        negative_shorts=negative_shorts,
    )


def _compact_parameter_entries(entries: list[HelpEntry]) -> list[HelpEntry]:
    compacted = [_compact_parameter_entry(entry) for entry in entries]
    option_sets = [frozenset(entry.all_options) for entry in compacted]
    visible: list[HelpEntry] = []
    for index, entry in enumerate(compacted):
        options = option_sets[index]
        if options and any(
            options < other for other_index, other in enumerate(option_sets) if other_index != index
        ):
            continue
        visible.append(entry)
    return visible


class AlignedPlainFormatter(PlainFormatter):
    """Plain help formatter that keeps wrapped description lines aligned."""

    def __call__(
        self,
        console: Console,
        options: Any,
        panel: HelpPanel,
    ) -> None:
        if panel.format == "parameter":
            panel = evolve(panel, entries=_compact_parameter_entries(panel.entries))
        super().__call__(console, options, panel)


ALIGNED_PLAIN_FORMATTER = AlignedPlainFormatter()

_HELP_SKIP_TOKENS = frozenset({"--help", "-h", "--advanced", "--mode"})


def _command_app_from_root(root_app: App, group_name: str) -> App | None:
    if group_name not in root_app:
        return None
    return root_app[group_name]


def group_apps_for_help(*, root_app: App) -> dict[str, App]:
    """Return group ``App`` objects addressable by ``GROUPS`` metadata keys."""

    group_apps = dict(group_apps_with_static_apps())
    for group_name in GROUPS:
        if group_name in group_apps:
            continue
        resolved = group_app(group_name)
        if resolved is None:
            resolved = _command_app_from_root(root_app, group_name)
        if resolved is not None:
            group_apps[group_name] = resolved
    return group_apps


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
    return [arg for arg in argv if arg not in _HELP_SKIP_TOKENS]


def try_render_curated_group_help(
    root_app: App,
    argv: Sequence[str],
    *,
    registered_groups: set[str],
    agent_mode: bool,
    advanced: bool = False,
) -> bool:
    """Render curated group help when argv requests it.

    Returns ``True`` when help was rendered (caller should exit successfully).
    """

    group_name = curated_group_help_target(
        argv,
        root_app=root_app,
        registered_groups=registered_groups,
    )
    if group_name is None:
        return False

    print_curated_group_help(
        root_app,
        argv,
        group_name,
        agent_mode=agent_mode,
        advanced=advanced,
    )
    return True


@contextmanager
def leaf_help_rendering_scope(
    argv: Sequence[str],
    *,
    root_app: App,
    registered_groups: set[str],
) -> Generator[None, None, None]:
    """Scope Cyclopts leaf help so parent group epilogues stay suppressed.

    Group-level help is handled by :func:`try_render_curated_group_help`; this
    context manager only affects subcommand (leaf) ``-h`` / ``--help`` requests.
    """

    if (
        curated_group_help_target(
            argv,
            root_app=root_app,
            registered_groups=registered_groups,
        )
        is not None
    ):
        yield
        return

    tokens = _help_tokens(argv)
    if len(tokens) < 2:
        yield
        return

    group_name = tokens[0]
    group_app = group_apps_for_help(root_app=root_app).get(group_name)
    if group_app is None:
        yield
        return

    saved_epilogue = group_app.help_epilogue
    group_app.help_epilogue = ""
    try:
        yield
    finally:
        group_app.help_epilogue = saved_epilogue


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
        return assemble_help_panels(root_app, tokens, help_format)


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
            panel = evolve(panel, entries=entries)

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
    formatter = ALIGNED_PLAIN_FORMATTER

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
    "ALIGNED_PLAIN_FORMATTER",
    "AlignedPlainFormatter",
    "curated_group_help_target",
    "group_apps_for_help",
    "leaf_help_rendering_scope",
    "print_curated_group_help",
    "try_render_curated_group_help",
]
