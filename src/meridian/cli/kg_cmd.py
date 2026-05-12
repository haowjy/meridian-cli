"""CLI handlers for `meridian kg` commands."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

from cyclopts import Parameter

from meridian.cli._cmd_utils import resolve_context_alias, resolve_fmt
from meridian.cli.app_tree import kg_app


@kg_app.default
def cmd_kg_root(
    path: Annotated[
        Path,
        Parameter(help="Directory to analyze (default: cwd)."),
    ] = Path("."),
) -> None:
    """Quick summary of a directory's document graph."""
    from meridian.lib.kg.graph import build_analysis
    from meridian.lib.kg.report import format_root_summary

    resolved = _require_dir(resolve_context_alias(path))
    result = build_analysis(
        root=resolved,
        include_backlinks=False,
        include_clusters=False,
    )
    print(format_root_summary(result, root=resolved))
    raise SystemExit(0)


@kg_app.command(name="graph")
def cmd_kg_graph(
    root: Annotated[
        Path,
        Parameter(help="Root directory or file to analyze (default: cwd)."),
    ] = Path("."),
    *,
    depth: Annotated[
        int,
        Parameter(name="--depth", help="Max link-hops to show in tree (default: 3)."),
    ] = 3,
    external: Annotated[
        bool,
        Parameter(name="--external", help="Show external URLs as leaf nodes in tree."),
    ] = False,
    exclude: Annotated[
        list[str] | None,
        Parameter(name="--exclude", help="Glob pattern to exclude (repeatable)."),
    ] = None,
    fmt: Annotated[
        str,
        Parameter(name="--format", help="Output format: text (default) or json."),
    ] = "text",
) -> None:
    """Show document link topology as an indented tree."""
    from meridian.lib.kg.graph import build_analysis
    from meridian.lib.kg.report import format_tree
    from meridian.lib.kg.serializer import serialize_analysis

    resolved = resolve_context_alias(root).resolve()
    if not resolved.exists():
        print(f"Error: path not found: {root}", file=sys.stderr)
        raise SystemExit(2)

    if resolved.is_dir():
        scan_root = resolved
        targeted_path = None
    else:
        scan_root = resolved.parent
        targeted_path = resolved

    result = build_analysis(
        root=scan_root,
        include_backlinks=False,
        include_clusters=False,
        targeted_path=targeted_path,
        exclude=exclude or None,
    )

    if resolve_fmt(fmt) == "json":
        import json

        print(json.dumps(serialize_analysis(result, scan_root), indent=2))
    else:
        print(
            format_tree(
                result,
                scan_root,
                depth=depth,
                show_external=external,
            )
        )

    raise SystemExit(1 if result.broken_links else 0)


@kg_app.command(name="check")
def cmd_kg_check(
    path: Annotated[
        Path,
        Parameter(help="File or directory to check for broken links (default: cwd)."),
    ] = Path("."),
    *,
    exclude: Annotated[
        list[str] | None,
        Parameter(name="--exclude", help="Glob pattern to exclude (repeatable)."),
    ] = None,
    fmt: Annotated[
        str,
        Parameter(name="--format", help="Output format: text (default) or json."),
    ] = "text",
    strict: Annotated[
        bool,
        Parameter(name="--strict", help="Treat warnings as errors."),
    ] = False,
) -> None:
    """Check for KG findings. Exit 1 if errors found."""
    from meridian.lib.kg.graph import build_check
    from meridian.lib.kg.report import format_check_findings
    from meridian.lib.kg.serializer import serialize_check_findings

    resolved = resolve_context_alias(path).resolve()
    if not resolved.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        raise SystemExit(2)

    if resolved.is_dir():
        root = resolved
        targeted_path = None
    else:
        root = resolved.parent
        targeted_path = resolved

    result = build_check(
        root=root,
        targeted_path=targeted_path,
        exclude=exclude or None,
        strict=strict,
    )

    if resolve_fmt(fmt) == "json":
        import json

        print(json.dumps(serialize_check_findings(result, resolved), indent=2))
    else:
        stdout, stderr = format_check_findings(result, root)
        if stdout:
            print(stdout)
        if stderr:
            print(stderr, file=sys.stderr)

    raise SystemExit(1 if result.has_errors else 0)


def _require_dir(path: Path) -> Path:
    """Resolve path and exit 2 if it does not exist or is not a directory."""
    resolved = path.resolve()
    if not resolved.exists():
        print(f"Error: path not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    if not resolved.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        raise SystemExit(2)
    return resolved


__all__ = [
    "cmd_kg_check",
    "cmd_kg_graph",
    "cmd_kg_root",
]
