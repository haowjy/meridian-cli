#!/usr/bin/env python3
"""Generate cli/command_groups.py from help_content.GROUPS (dev utility)."""

from __future__ import annotations

from pathlib import Path

HUMAN_ORDER = [
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
]

META_BASE: dict[str, dict[str, object]] = {
    "spawn": {
        "agent_root_description": (
            "Hand a task to a subagent. Runs in the background; you keep working\n"
            "and collect the result later. (`wait`, `report`)"
        ),
        "agent_root": True,
        "registration_bucket": "spawn",
        "has_group_app": True,
    },
    "session": {
        "agent_root_description": (
            "Read the full transcript of any spawn — what it did, what it found —\n"
            "so you build on it instead of redoing it."
        ),
        "agent_root": True,
        "registration_bucket": "session",
        "has_group_app": True,
    },
    "work": {
        "agent_root_description": (
            "Tie related spawns to one goal with a shared folder and history, so\n"
            "a multi-step effort holds together across handoffs."
        ),
        "agent_root": True,
        "registration_bucket": "work",
        "has_group_app": True,
    },
    "context": {
        "agent_root_description": (
            "Show the folders (as env vars) where shared files and project\n"
            "knowledge live, so you read and write them in the right place."
        ),
        "agent_root": True,
        "registration_bucket": "misc",
        "has_group_app": True,
    },
    "mars": {
        "agent_root_description": (
            "List the models, agents, and skills available, so you pick real ones\n"
            "when you delegate. (`meridian mars models list`)"
        ),
        "agent_root": True,
    },
    "ext": {
        "agent_root_description": "Run project-specific extension commands.",
        "agent_root": True,
        "registration_bucket": "ext",
        "has_group_app": True,
    },
    "config": {"registration_bucket": "config", "has_group_app": True},
    "doctor": {"registration_bucket": "doctor"},
    "hooks": {"registration_bucket": "hooks", "has_group_app": True},
    "sync": {"registration_bucket": "sync"},
    "models": {"registration_bucket": "models", "has_group_app": True},
    "streaming": {"registration_bucket": "misc", "has_group_app": True},
    "test": {"registration_bucket": "misc", "has_group_app": True},
    "workspace": {"registration_bucket": "workspace", "has_group_app": True},
    "kg": {"registration_bucket": "kg", "has_group_app": True},
    "mermaid": {"registration_bucket": "mermaid", "has_group_app": True},
    "telemetry": {"registration_bucket": "telemetry", "has_group_app": True},
    "completion": {"registration_bucket": "misc", "has_group_app": True},
    "chat": {"registration_bucket": "chat"},
    "bootstrap": {"registration_bucket": "bootstrap"},
    "migrate": {"registration_bucket": "migrate"},
    "qi": {"registration_bucket": "qi", "has_group_app": True},
}

for idx, name in enumerate(HUMAN_ORDER):
    META_BASE.setdefault(name, {})["human_root_order"] = idx


def main() -> None:
    from meridian.cli.help_content import GROUPS, GroupHelp

    def repr_group_help(name: str, gh: GroupHelp) -> str:
        lines = [f'    "{name}": GroupHelp(']
        lines.append(f"        summary={gh.summary!r},")
        if gh.long_help is not None:
            lines.append(f"        long_help={gh.long_help!r},")
        if gh.examples:
            lines.append(f"        examples={gh.examples!r},")
        if gh.agent_notes is not None:
            lines.append(f"        agent_notes={gh.agent_notes!r},")
        if gh.agent_notes_brief is not None:
            lines.append(f"        agent_notes_brief={gh.agent_notes_brief!r},")
        if gh.agent_subcommands is not None:
            lines.append(f"        agent_subcommands={gh.agent_subcommands!r},")
        if gh.agent_core_subcommands is not None:
            lines.append(f"        agent_core_subcommands={gh.agent_core_subcommands!r},")
        lines.append("    ),")
        return "\n".join(lines)

    help_lines = ["_GROUP_HELP: dict[str, GroupHelp] = {"]
    for name, gh in GROUPS.items():
        help_lines.append(repr_group_help(name, gh))
    help_lines.append("}")

    spec_lines = ["COMMAND_GROUP_SPECS: dict[str, CommandGroupSpec] = {"]
    for name in GROUPS:
        meta = META_BASE.get(name, {})
        spec_lines.append(f'    "{name}": CommandGroupSpec(')
        spec_lines.append(f'        help=_GROUP_HELP["{name}"],')
        if meta.get("agent_root_description"):
            spec_lines.append(f'        agent_root_description={meta["agent_root_description"]!r},')
        if meta.get("agent_root"):
            spec_lines.append("        agent_root=True,")
        if meta.get("human_root_order") is not None:
            spec_lines.append(f'        human_root_order={meta["human_root_order"]},')
        if meta.get("registration_bucket"):
            spec_lines.append(f'        registration_bucket={meta["registration_bucket"]!r},')
        if meta.get("has_group_app"):
            spec_lines.append("        has_group_app=True,")
        spec_lines.append("    ),")
    spec_lines.append("}")

    render_fn = '''def render_group_help(group_name: str, *, agent_mode: bool, advanced: bool = False) -> str:
    """Render the group description for human or agent help mode."""

    group = GROUPS[group_name]
    sections = [group.long_help or group.summary]
    lean = agent_mode and not advanced and group.agent_notes_brief is not None

    if group.examples and not lean:
        example_lines: list[str] = []
        for invocation, note in group.examples:
            if note:
                example_lines.append(f"  {invocation}  # {note}")
            else:
                example_lines.append(f"  {invocation}")
        sections.append("Usage examples:\\n\\n" + "\\n\\n".join(example_lines))

    if lean:
        agent_notes_brief = group.agent_notes_brief
        if agent_notes_brief is not None:
            sections.append("Playbook:\\n\\n" + agent_notes_brief.rstrip())
    elif agent_mode and group.agent_notes:
        sections.append("Agent Notes:\\n\\n" + group.agent_notes.rstrip())

    return "\\n\\n".join(sections)'''

    template = f'''"""Authoritative registry for CLI command groups.

Owns help content, root-help curation, group app linkage, and lazy-registration
bucket mapping. Root help, curated help, and command registration derive from
``COMMAND_GROUP_SPECS`` — do not duplicate parallel tables elsewhere.
"""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cyclopts import App


@dataclass(frozen=True)
class GroupHelp:
    """Help content shared by root help, group help, and agent curation."""

    summary: str
    long_help: str | None = None
    examples: tuple[tuple[str, str], ...] = ()
    agent_notes: str | None = None
    agent_notes_brief: str | None = None
    agent_subcommands: tuple[str, ...] | None = None
    agent_core_subcommands: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CommandGroupSpec:
    """Metadata for one top-level CLI command group."""

    help: GroupHelp
    agent_root_description: str | None = None
    agent_root: bool = False
    human_root_order: int | None = None
    registration_bucket: str | None = None
    has_group_app: bool = False


_group_apps: dict[str, App] = {{}}


{chr(10).join(help_lines)}

{chr(10).join(spec_lines)}

GROUPS: dict[str, GroupHelp] = {{name: spec.help for name, spec in COMMAND_GROUP_SPECS.items()}}

AGENT_ROOT_COMMANDS: tuple[str, ...] = tuple(
    name for name, spec in COMMAND_GROUP_SPECS.items() if spec.agent_root
)

HUMAN_ROOT_ORDER: tuple[str, ...] = tuple(
    name
    for name, _ in sorted(
        (
            (name, spec.human_root_order)
            for name, spec in COMMAND_GROUP_SPECS.items()
            if spec.human_root_order is not None
        ),
        key=lambda item: item[1],
    )
)

AGENT_DESCRIPTION_OVERRIDES: dict[str, str] = {{
    name: spec.agent_root_description
    for name, spec in COMMAND_GROUP_SPECS.items()
    if spec.agent_root_description is not None
}}

GROUP_DESCRIPTIONS: dict[str, str] = {{name: spec.help.summary for name, spec in COMMAND_GROUP_SPECS.items()}}

COMMAND_REGISTRATION: dict[str, str] = {{
    name: spec.registration_bucket
    for name, spec in COMMAND_GROUP_SPECS.items()
    if spec.registration_bucket is not None
}}

AGENT_VISIBLE_SUBCOMMANDS: dict[str, tuple[str, ...]] = {{
    name: spec.help.agent_subcommands
    for name, spec in COMMAND_GROUP_SPECS.items()
    if spec.help.agent_subcommands is not None
}}

AGENT_CORE_SUBCOMMANDS: dict[str, tuple[str, ...]] = {{
    name: spec.help.agent_core_subcommands
    for name, spec in COMMAND_GROUP_SPECS.items()
    if spec.help.agent_core_subcommands is not None
}}

AGENT_HELP_SUPPLEMENTS: dict[str, str] = {{
    name: spec.help.agent_notes
    for name, spec in COMMAND_GROUP_SPECS.items()
    if spec.help.agent_notes is not None
}}


def register_group_app(name: str, app: App) -> None:
    """Record a Cyclopts group ``App`` for curated help lookup."""

    _group_apps[name] = app


def group_app(name: str) -> App | None:
    return _group_apps.get(name)


def group_apps_with_static_apps() -> dict[str, App]:
    """Return group apps registered at import time (no root app walk)."""

    return dict(_group_apps)


def registration_buckets() -> dict[str, frozenset[str]]:
    """Map lazy-registration bucket names to command group names."""

    buckets: dict[str, set[str]] = {{}}
    for name, spec in COMMAND_GROUP_SPECS.items():
        if spec.registration_bucket is None:
            continue
        buckets.setdefault(spec.registration_bucket, set()).add(name)
    return {{bucket: frozenset(names) for bucket, names in buckets.items()}}


def supports_advanced_help(group_name: str) -> bool:
    spec = COMMAND_GROUP_SPECS.get(group_name)
    return spec is not None and spec.help.agent_core_subcommands is not None


{render_fn}

__all__ = [
    "AGENT_CORE_SUBCOMMANDS",
    "AGENT_DESCRIPTION_OVERRIDES",
    "AGENT_HELP_SUPPLEMENTS",
    "AGENT_ROOT_COMMANDS",
    "AGENT_VISIBLE_SUBCOMMANDS",
    "COMMAND_GROUP_SPECS",
    "COMMAND_REGISTRATION",
    "GROUPS",
    "GROUP_DESCRIPTIONS",
    "HUMAN_ROOT_ORDER",
    "CommandGroupSpec",
    "GroupHelp",
    "group_app",
    "group_apps_with_static_apps",
    "register_group_app",
    "registration_buckets",
    "render_group_help",
    "supports_advanced_help",
]
'''
    out = Path("src/meridian/cli/command_groups.py")
    out.write_text(template)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
