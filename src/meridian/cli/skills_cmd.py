"""CLI command for Meridian's bundled, on-demand skills."""

from __future__ import annotations

from typing import Annotated

from cyclopts import Parameter

from meridian.cli.app_tree import app
from meridian.cli.invocation import current_output_sink
from meridian.lib.bundled_skills import list_bundled_skills, read_bundled_skill


@app.command(name="skills")
def cmd_skills(
    name: Annotated[
        str,
        Parameter(help="Use 'list' to show skills, or give a skill name to print SKILL.md."),
    ],
    resource_path: Annotated[
        str | None,
        Parameter(help="Optional path inside that skill, such as resources/lifecycle.md."),
    ] = None,
) -> None:
    """Read Meridian's bundled operational skills on demand."""

    sink = current_output_sink()
    if name == "list":
        if resource_path is not None:
            sink.error("meridian skills list does not accept a resource path", exit_code=2)
            raise SystemExit(2)
        sink.result(
            "\n".join(
                f"{skill.name}: {skill.description}" for skill in list_bundled_skills()
            )
        )
        return

    try:
        content = read_bundled_skill(name, resource_path)
    except ValueError as exc:
        sink.error(str(exc), exit_code=2)
        raise SystemExit(2) from None
    sink.result(content.rstrip())


__all__ = ["cmd_skills"]
