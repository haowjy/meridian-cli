"""CLI handlers for `meridian qi` commands."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

from cyclopts import Parameter

from meridian.cli._cmd_utils import resolve_fmt
from meridian.cli.app_tree import qi_app


@qi_app.default
def cmd_qi_root(
    path: Annotated[
        Path,
        Parameter(help="Directory to summarize (default: cwd)."),
    ] = Path("."),
) -> None:
    """Quick summary of inline knowledge coverage for a directory."""
    from meridian.lib.ops.qi import qi_summary_sync

    resolved = path.resolve()
    if not resolved.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    if not resolved.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        raise SystemExit(2)

    result = qi_summary_sync(resolved)
    print(result.format_text())
    raise SystemExit(0)


@qi_app.command(name="graph")
def cmd_qi_graph(
    path: Annotated[
        Path,
        Parameter(help="File or directory to inspect (default: cwd)."),
    ] = Path("."),
    *,
    fmt: Annotated[
        str,
        Parameter(name="--format", help="Output format: text (default) or json."),
    ] = "text",
) -> None:
    """Show inline knowledge boundary for a path."""
    from meridian.lib.config.project_root import resolve_project_root
    from meridian.lib.ops.qi import qi_show_sync

    resolved = path.resolve()
    if not resolved.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        raise SystemExit(2)

    project_root = resolve_project_root()
    result = qi_show_sync(resolved, project_root)

    if resolve_fmt(fmt) == "json":
        import json

        print(json.dumps(result.model_dump(), indent=2))
    else:
        print(result.format_text())

    raise SystemExit(0)


@qi_app.command(name="check")
def cmd_qi_check(
    path: Annotated[
        Path,
        Parameter(help="Directory to check (default: cwd)."),
    ] = Path("."),
    *,
    fmt: Annotated[
        str,
        Parameter(name="--format", help="Output format: text (default) or json."),
    ] = "text",
) -> None:
    """Check inline knowledge health."""
    from meridian.lib.ops.qi import qi_check_sync

    resolved = path.resolve()
    if not resolved.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    if not resolved.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        raise SystemExit(2)

    result = qi_check_sync(resolved)

    if resolve_fmt(fmt) == "json":
        import json

        print(json.dumps(result.model_dump(), indent=2))
    else:
        print(result.format_text())

    raise SystemExit(1 if result.has_errors else 0)


__all__ = [
    "cmd_qi_check",
    "cmd_qi_graph",
    "cmd_qi_root",
]
