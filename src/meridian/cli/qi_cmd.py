"""CLI handlers for `meridian qi` commands."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

from cyclopts import Parameter

from meridian.cli.app_tree import qi_app


@qi_app.default
def cmd_qi_root(
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
    from meridian.cli.main import get_global_options
    from meridian.lib.config.project_root import resolve_project_root
    from meridian.lib.ops.qi import qi_show_sync

    resolved = path.resolve()
    if not resolved.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        raise SystemExit(2)

    project_root = resolve_project_root()
    result = qi_show_sync(resolved, project_root)

    effective_fmt = "json" if get_global_options().output.format == "json" else fmt

    if effective_fmt == "json":
        import json

        print(json.dumps(result.model_dump(), indent=2))
    else:
        print(result.format_text())

    raise SystemExit(0)


@qi_app.command(name="list")
def cmd_qi_list(
    root: Annotated[
        Path,
        Parameter(help="Root directory to scan (default: cwd)."),
    ] = Path("."),
    *,
    fmt: Annotated[
        str,
        Parameter(name="--format", help="Output format: text (default) or json."),
    ] = "text",
) -> None:
    """List all inline knowledge locations under a directory."""
    from meridian.cli.main import get_global_options
    from meridian.lib.ops.qi import qi_list_sync

    resolved = root.resolve()
    if not resolved.exists():
        print(f"Error: path not found: {root}", file=sys.stderr)
        raise SystemExit(2)
    if not resolved.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        raise SystemExit(2)

    result = qi_list_sync(resolved)

    effective_fmt = "json" if get_global_options().output.format == "json" else fmt

    if effective_fmt == "json":
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
    from meridian.cli.main import get_global_options
    from meridian.lib.ops.qi import qi_check_sync

    resolved = path.resolve()
    if not resolved.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    if not resolved.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        raise SystemExit(2)

    result = qi_check_sync(resolved)

    effective_fmt = "json" if get_global_options().output.format == "json" else fmt

    if effective_fmt == "json":
        import json

        print(json.dumps(result.model_dump(), indent=2))
    else:
        print(result.format_text())

    raise SystemExit(1 if result.has_errors else 0)


__all__ = [
    "cmd_qi_check",
    "cmd_qi_list",
    "cmd_qi_root",
]
