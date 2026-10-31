"""CLI command for Meridian's bundled, on-demand skills."""

from __future__ import annotations

import sys
from typing import Annotated

from cyclopts import App, Parameter

from meridian.cli.invocation import current_output_sink, get_global_options
from meridian.cli.utils import resolve_cli_project_root
from meridian.lib.bundled_skills import (
    SYNC_PENDING_MESSAGE,
    WHEEL,
    list_bundled_skills,
    load_bundled_skill,
    provide_bundled_skills,
    read_bundled_skill_resource,
    report_provide_refusal,
)
from meridian.lib.catalog.mars_roots import resolve_mars_executable, resolve_mars_roots
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
                [{"name": skill.name, "description": skill.description} for skill in skills]
            )
        else:
            sink.result("\n".join(f"{skill.name}: {skill.description}" for skill in skills))
        return

    known_names = {skill.name for skill in list_bundled_skills()}
    if name not in known_names:
        available = ", ".join(sorted(known_names))
        sink.error(f"unknown Meridian skill {name!r}; choose one of: {available}", exit_code=2)
        raise SystemExit(2)

    roots = resolve_mars_roots(resolve_cli_project_root())
    source = WHEEL
    if roots.scope == "project":
        source = provide_bundled_skills(
            roots.primary,
            executable=resolve_mars_executable(),
        )
        if source.sync_pending:
            print(SYNC_PENDING_MESSAGE, file=sys.stderr)
        report_provide_refusal(source)

    try:
        if resource_path is None:
            content = render_skill_block(load_bundled_skill(name, source))
        else:
            content = read_bundled_skill_resource(name, resource_path, source)
    except ValueError as exc:
        sink.error(str(exc), exit_code=2)
        raise SystemExit(2) from None
    sink.result(content.rstrip())


def register_skills_commands(app: App) -> None:
    """Register the bundled-skills root command."""

    app.command(cmd_skills, name="skills")


__all__ = ["cmd_skills", "register_skills_commands"]
