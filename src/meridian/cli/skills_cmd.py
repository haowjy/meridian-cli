"""CLI command for Meridian's bundled, on-demand skills."""

from __future__ import annotations

from typing import Annotated

from cyclopts import App, Parameter

from meridian.cli.invocation import current_output_sink, get_global_options
from meridian.lib.bundled_skills import (
    list_bundled_skills,
    load_bundled_skill,
    read_bundled_skill_resource,
)
from meridian.lib.launch.prompt_context import render_skill_block


def cmd_skills(
    name: Annotated[
        str,
        Parameter(help="Use 'list' to show skills, or give a skill name to load it."),
    ],
    resource_path: Annotated[
        str | None,
        Parameter(help="Optional path inside that skill, such as resources/delivery.md."),
    ] = None,
) -> None:
    """Read Meridian's bundled operational skills on demand."""

    sink = current_output_sink()
    if name == "list":
        if resource_path is not None:
            sink.error("meridian skills list does not accept a resource path", exit_code=2)
            raise SystemExit(2)
        skills = list_bundled_skills()
        if get_global_options().output.format == "json":
            sink.result(
                [
                    {"name": skill.name, "description": skill.description}
                    for skill in skills
                ]
            )
        else:
            sink.result("\n".join(f"{skill.name}: {skill.description}" for skill in skills))
        return

    try:
        if resource_path is None:
            content = render_skill_block(load_bundled_skill(name))
        else:
            content = read_bundled_skill_resource(name, resource_path)
    except ValueError as exc:
        sink.error(str(exc), exit_code=2)
        raise SystemExit(2) from None
    sink.result(content.rstrip())


def register_skills_commands(app: App) -> None:
    """Register the bundled-skills root command."""

    app.command(cmd_skills, name="skills")


__all__ = ["cmd_skills", "register_skills_commands"]
